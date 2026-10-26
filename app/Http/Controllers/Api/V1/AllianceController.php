<?php

namespace OGame\Http\Controllers\Api\V1;

use Exception;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OGame\Exceptions\ApiException;
use OGame\Models\Alliance;
use OGame\Models\AllianceApplication;
use OGame\Models\AllianceRank;
use OGame\Models\User;
use OGame\Services\AllianceService;
use OGame\Services\PlayerService;

/**
 * Alliances: look at them, found one, apply, handle applications, leave, kick, broadcast.
 * A thin layer over AllianceService, the same code as the web UI, so the permission rules
 * (a founder can do everything, other members only what their rank allows), the tag and name
 * limits and the leave cooldown are the game's own. The service reports problems as plain
 * exceptions; they are turned into stable error codes here.
 */
class AllianceController extends ApiController
{
    /** Permissions reported to the client as "my_permissions". */
    private const PERMISSIONS = [
        AllianceRank::PERMISSION_SEE_APPLICATIONS,
        AllianceRank::PERMISSION_EDIT_APPLICATIONS,
        AllianceRank::PERMISSION_SEE_MEMBERS,
        AllianceRank::PERMISSION_KICK_USER,
        AllianceRank::PERMISSION_SEND_CIRCULAR_MSG,
        AllianceRank::PERMISSION_MANAGE_ALLY,
        AllianceRank::PERMISSION_DELETE_ALLY,
    ];

    /**
     * Alliances of the universe, largest first. Query: open_only (0/1), page, per_page (1-100, default 25).
     *
     * @throws ApiException
     */
    public function index(Request $request): JsonResponse
    {
        $input = $this->validated($request->query(), [
            'open_only' => ['nullable', 'boolean'],
            'page' => ['nullable', 'integer', 'min:1'],
            'per_page' => ['nullable', 'integer', 'min:1', 'max:100'],
        ]);

        $query = Alliance::query()->withCount('members');
        if (!empty($input['open_only'])) {
            $query->where('is_open', true);
        }
        $paginator = $query->orderByDesc('members_count')->orderBy('id')
            ->paginate((int)($input['per_page'] ?? 25), ['*'], 'page', (int)($input['page'] ?? 1));

        $alliances = [];
        foreach ($paginator->items() as $alliance) {
            $alliances[] = $this->publicArray($alliance, (int)$alliance->members_count);
        }

        return $this->ok([
            'alliances' => $alliances,
            'page' => $paginator->currentPage(),
            'per_page' => $paginator->perPage(),
            'total' => $paginator->total(),
            'total_pages' => $paginator->lastPage(),
        ]);
    }

    /**
     * My alliance: its data, members, ranks, my rank and permissions and, when my rank may see them,
     * the open applications. "alliance" is null when I am in none.
     *
     * @throws ApiException
     */
    public function mine(PlayerService $player, AllianceService $alliances): JsonResponse
    {
        $user = $player->getUser();
        if ($user->alliance_id === null) {
            return $this->ok(['alliance' => null]);
        }

        $alliance = $alliances->getAllianceById($user->alliance_id);
        $member = $alliances->getAllianceMember($user->alliance_id, $player->getId());
        if ($alliance === null || $member === null) {
            return $this->ok(['alliance' => null]);
        }

        $members = [];
        foreach ($alliances->getAllianceMembers($alliance->id) as $row) {
            $members[] = [
                'user_id' => $row->user_id,
                'username' => $row->user->username ?? '',
                'rank_id' => $row->rank_id,
                'rank' => $row->isFounder() ? $alliance->founder_rank_name : ($row->rank->rank_name ?? null),
                'is_founder' => $row->isFounder(),
                'joined_at' => $row->joined_at?->timestamp,
                'points' => (int)($row->user->highscore->general ?? 0),
            ];
        }

        $ranks = [];
        foreach ($alliance->ranks()->orderBy('sort_order')->get() as $rank) {
            $ranks[] = ['id' => $rank->id, 'name' => $rank->rank_name, 'permissions' => $rank->permissions ?? []];
        }

        $permissions = [];
        foreach (self::PERMISSIONS as $permission) {
            if ($member->hasPermission($permission)) {
                $permissions[] = $permission;
            }
        }

        $applications = null;
        if ($member->hasPermission(AllianceRank::PERMISSION_SEE_APPLICATIONS)) {
            $applications = [];
            foreach ($alliances->getPendingApplications($alliance->id) as $application) {
                $applications[] = $this->applicationArray($application);
            }
        }

        return $this->ok(['alliance' => array_merge($this->publicArray($alliance, count($members)), [
            'internal_text' => $alliance->internal_text,
            'members' => $members,
            'ranks' => $ranks,
            'my_user_id' => $player->getId(),
            'my_rank_id' => $member->rank_id,
            'is_founder' => $member->isFounder(),
            'my_permissions' => $permissions,
            'applications' => $applications,
        ])]);
    }

    /**
     * Public information about one alliance, with its members.
     *
     * @throws ApiException
     */
    public function show(AllianceService $alliances, int $id): JsonResponse
    {
        $alliance = $alliances->getAllianceById($id);
        if ($alliance === null) {
            throw new ApiException('alliance_not_found', 'Alliance not found.', 404);
        }

        $members = [];
        foreach ($alliances->getAllianceMembers($alliance->id) as $row) {
            $members[] = ['user_id' => $row->user_id, 'username' => $row->user->username ?? '', 'is_founder' => $row->isFounder()];
        }

        return $this->ok(['alliance' => array_merge($this->publicArray($alliance, count($members)), ['members' => $members])]);
    }

    /**
     * Found an alliance. Body: tag (3-8 characters, unique), name (3-30 characters, unique).
     *
     * @throws ApiException
     */
    public function create(Request $request, PlayerService $player, AllianceService $alliances): JsonResponse
    {
        $input = $this->validated($request->all(), [
            'tag' => ['required', 'string', 'min:3', 'max:8'],
            'name' => ['required', 'string', 'min:3', 'max:30'],
        ]);
        $this->assertNotInVacationMode($player);

        if ($player->getUser()->alliance_id !== null) {
            throw new ApiException('already_in_alliance', 'The player is already in an alliance.', 422);
        }
        if ($alliances->getAllianceByTag($input['tag']) !== null) {
            throw new ApiException('tag_taken', 'Alliance tag is already taken.', 422);
        }
        if (Alliance::where('alliance_name', $input['name'])->exists()) {
            throw new ApiException('name_taken', 'Alliance name is already taken.', 422);
        }

        try {
            $alliance = $alliances->createAlliance($player->getId(), $input['tag'], $input['name']);
        } catch (Exception $e) {
            throw $this->refused($e);
        }

        return $this->ok(['alliance' => $this->publicArray($alliance, 1)], 201);
    }

    /**
     * Apply to an open alliance. Body: text (optional, up to 2000 characters).
     *
     * @throws ApiException
     */
    public function apply(Request $request, PlayerService $player, AllianceService $alliances, int $id): JsonResponse
    {
        $input = $this->validated($request->all(), [
            'text' => ['nullable', 'string', 'max:2000'],
        ]);
        $this->assertNotInVacationMode($player);

        $alliance = $alliances->getAllianceById($id);
        if ($alliance === null) {
            throw new ApiException('alliance_not_found', 'Alliance not found.', 404);
        }
        if ($player->getUser()->alliance_id !== null) {
            throw new ApiException('already_in_alliance', 'The player is already in an alliance.', 422);
        }
        if (!$alliance->is_open) {
            throw new ApiException('alliance_closed', 'Alliance is not accepting applications.', 422);
        }
        $pending = AllianceApplication::where('alliance_id', $id)
            ->where('user_id', $player->getId())
            ->where('status', AllianceApplication::STATUS_PENDING)
            ->exists();
        if ($pending) {
            throw new ApiException('application_pending', 'You already have a pending application to this alliance.', 422);
        }

        try {
            $application = $alliances->applyToAlliance($player->getId(), $id, $input['text'] ?? null);
        } catch (Exception $e) {
            throw $this->refused($e);
        }

        return $this->ok(['application' => $this->applicationArray($application)], 201);
    }

    /**
     * Accept an application to my alliance (needs the edit_applications permission, the founder has it).
     *
     * @throws ApiException
     */
    public function accept(PlayerService $player, AllianceService $alliances, int $id): JsonResponse
    {
        $application = $this->ownApplication($player, $id);

        try {
            $alliances->acceptApplication($application->id, $player->getId());
        } catch (Exception $e) {
            throw $this->refused($e);
        }

        return $this->ok(['application' => $this->applicationArray($application->fresh() ?? $application)]);
    }

    /**
     * Reject an application to my alliance (needs the edit_applications permission).
     *
     * @throws ApiException
     */
    public function reject(PlayerService $player, AllianceService $alliances, int $id): JsonResponse
    {
        $application = $this->ownApplication($player, $id);

        try {
            $alliances->rejectApplication($application->id, $player->getId());
        } catch (Exception $e) {
            throw $this->refused($e);
        }

        return $this->ok(['application' => $this->applicationArray($application->fresh() ?? $application)]);
    }

    /**
     * Leave my alliance. The founder cannot, and the game sets a cooldown before joining or founding another.
     *
     * @throws ApiException
     */
    public function leave(PlayerService $player, AllianceService $alliances): JsonResponse
    {
        if ($player->getUser()->alliance_id === null) {
            throw new ApiException('not_in_alliance', 'The player is not in an alliance.', 422);
        }

        try {
            $alliances->leaveAlliance($player->getId());
        } catch (Exception $e) {
            throw $this->refused($e);
        }

        return $this->ok(['left' => true]);
    }

    /**
     * Kick a member (needs the kick_user permission; the founder cannot be kicked). Body: user_id.
     *
     * @throws ApiException
     */
    public function kick(Request $request, PlayerService $player, AllianceService $alliances): JsonResponse
    {
        $input = $this->validated($request->all(), [
            'user_id' => ['required', 'integer', 'min:1'],
        ]);
        $allianceId = $player->getUser()->alliance_id;
        if ($allianceId === null) {
            throw new ApiException('not_in_alliance', 'The player is not in an alliance.', 422);
        }

        try {
            $alliances->kickMember($allianceId, (int)$input['user_id'], $player->getId());
        } catch (Exception $e) {
            throw $this->refused($e);
        }

        return $this->ok(['kicked' => (int)$input['user_id']]);
    }

    /**
     * Send a circular message to the other members of my alliance (needs the send_circular_msg
     * permission, the founder has it). Body: message (up to 2000 characters). The members receive it
     * as a game message (see GET /messages, category "messages"). For free talk between all members
     * use the alliance chat (POST /chat/send) instead.
     *
     * @throws ApiException
     */
    public function broadcast(Request $request, PlayerService $player, AllianceService $alliances): JsonResponse
    {
        $input = $this->validated($request->all(), [
            'message' => ['required', 'string', 'min:1', 'max:2000'],
        ]);
        $allianceId = $player->getUser()->alliance_id;
        if ($allianceId === null) {
            throw new ApiException('not_in_alliance', 'The player is not in an alliance.', 422);
        }

        try {
            $alliances->sendBroadcastMessage($allianceId, $player->getId(), $input['message'], ['-1']);
        } catch (Exception $e) {
            throw $this->refused($e);
        }

        return $this->ok(['sent' => true]);
    }

    /**
     * A pending application that belongs to my alliance. Anything else is a 404.
     *
     * @throws ApiException
     */
    private function ownApplication(PlayerService $player, int $id): AllianceApplication
    {
        $application = AllianceApplication::find($id);
        $allianceId = $player->getUser()->alliance_id;
        if ($application === null || $allianceId === null || $application->alliance_id !== $allianceId) {
            throw new ApiException('application_not_found', 'Application not found.', 404);
        }
        if (!$application->isPending()) {
            throw new ApiException('application_not_pending', 'The application was already handled.', 422);
        }

        return $application;
    }

    /**
     * Turn an exception of AllianceService into an API error with a stable code.
     */
    private function refused(Exception $e): ApiException
    {
        $message = $e->getMessage();
        if (str_contains($message, 'permission')) {
            return new ApiException('no_permission', $message, 403);
        }
        if (str_contains($message, 'must wait')) {
            return new ApiException('alliance_cooldown', $message, 422);
        }
        if (str_contains($message, 'already in an alliance')) {
            return new ApiException('already_in_alliance', $message, 422);
        }
        if (str_contains($message, 'Member not found')) {
            return new ApiException('member_not_found', $message, 404);
        }
        if (str_contains($message, 'Founder') || str_contains($message, 'founder')) {
            return new ApiException('founder_protected', $message, 422);
        }

        return new ApiException('alliance_refused', $message, 422);
    }

    /**
     * @return array<string, mixed>
     */
    private function publicArray(Alliance $alliance, int $memberCount): array
    {
        return [
            'id' => $alliance->id,
            'tag' => $alliance->alliance_tag,
            'name' => $alliance->alliance_name,
            'open' => (bool)$alliance->is_open,
            'founder_user_id' => $alliance->founder_user_id,
            'member_count' => $memberCount,
            'external_text' => $alliance->external_text,
            'application_text' => $alliance->application_text,
            'homepage_url' => $alliance->homepage_url,
        ];
    }

    /**
     * @return array<string, mixed>
     */
    private function applicationArray(AllianceApplication $application): array
    {
        $status = 'pending';
        if ($application->isAccepted()) {
            $status = 'accepted';
        } elseif ($application->isRejected()) {
            $status = 'rejected';
        }

        return [
            'id' => $application->id,
            'alliance_id' => $application->alliance_id,
            'user_id' => $application->user_id,
            'username' => User::find($application->user_id)?->username ?? '',
            'text' => $application->application_message,
            'status' => $status,
            'created_at' => $application->created_at?->timestamp,
        ];
    }
}
