<?php

namespace OGame\Http\Controllers\Api\V1;

use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OGame\Exceptions\ApiException;
use OGame\Factories\GameMessageFactory;
use OGame\Models\BattleReport;
use OGame\Models\EspionageReport;
use OGame\Models\Message;
use OGame\Services\PlayerService;
use Throwable;

/**
 * The player's messages, including the structured data of spy reports and battle reports,
 * so an agent can act on what it found out.
 */
class MessageController extends ApiController
{
    /** API category => [game tab, game subtab]. */
    private const CATEGORIES = [
        'espionage' => ['fleets', 'espionage'],
        'combat' => ['fleets', 'combat_reports'],
        'expeditions' => ['fleets', 'expeditions'],
        'transport' => ['fleets', 'transport'],
        'fleet_other' => ['fleets', 'other'],
        'messages' => ['communication', 'messages'],
        'information' => ['communication', 'information'],
        'economy' => ['economy', 'economy'],
        'universe' => ['universe', 'universe'],
        'system' => ['system', 'system'],
    ];

    /**
     * List messages, newest first.
     *
     * Query: category (see docs), unread_only (0/1), page, per_page (1-50, default 20), mark_read (0/1).
     * Messages are only marked as read when mark_read=1 is given.
     *
     * @throws ApiException
     */
    public function index(Request $request, PlayerService $player): JsonResponse
    {
        $input = $this->validated($request->query(), [
            'category' => ['nullable', 'string', 'in:' . implode(',', array_keys(self::CATEGORIES))],
            'unread_only' => ['nullable', 'boolean'],
            'page' => ['nullable', 'integer', 'min:1'],
            'per_page' => ['nullable', 'integer', 'min:1', 'max:50'],
            'mark_read' => ['nullable', 'boolean'],
        ]);

        $query = Message::where('user_id', $player->getId());
        if (!empty($input['category'])) {
            [$tab, $subtab] = self::CATEGORIES[$input['category']];
            $query->whereIn('key', GameMessageFactory::GetGameMessageKeysByTab($tab, $subtab));
        }
        if (!empty($input['unread_only'])) {
            $query->where('viewed', 0);
        }

        $paginator = $query->orderByDesc('created_at')->orderByDesc('id')
            ->paginate((int)($input['per_page'] ?? 20), ['*'], 'page', (int)($input['page'] ?? 1));

        $messages = [];
        foreach ($paginator->items() as $message) {
            $messages[] = $this->messageArray($message, false);
            if (!empty($input['mark_read']) && !$message->viewed) {
                $message->viewed = 1;
                $message->save();
            }
        }

        return $this->ok([
            'messages' => $messages,
            'page' => $paginator->currentPage(),
            'per_page' => $paginator->perPage(),
            'total' => $paginator->total(),
            'total_pages' => $paginator->lastPage(),
        ]);
    }

    /**
     * One message with its full text and, for spy and battle reports, the structured report.
     *
     * @throws ApiException
     */
    public function show(PlayerService $player, int $message): JsonResponse
    {
        return $this->ok(['message' => $this->messageArray($this->ownMessage($player, $message), true)]);
    }

    /**
     * Mark one message as read.
     *
     * @throws ApiException
     */
    public function markRead(PlayerService $player, int $message): JsonResponse
    {
        $model = $this->ownMessage($player, $message);
        if (!$model->viewed) {
            $model->viewed = 1;
            $model->save();
        }

        return $this->ok(['message' => $this->messageArray($model, false)]);
    }

    /**
     * @throws ApiException
     */
    private function ownMessage(PlayerService $player, int $id): Message
    {
        $message = Message::where('id', $id)->where('user_id', $player->getId())->first();
        if ($message === null) {
            throw new ApiException('message_not_found', 'Message not found.', 404);
        }

        return $message;
    }

    /**
     * @return array<string, mixed>
     */
    private function messageArray(Message $message, bool $full): array
    {
        $subject = $message->key;
        $body = '';
        try {
            $gameMessage = GameMessageFactory::createGameMessage($message);
            $subject = $this->plainText($gameMessage->getSubject());
            $body = $this->plainText($full ? $gameMessage->getBodyFull() : $gameMessage->getBody());
        } catch (Throwable) {
            // A message whose text cannot be rendered is still listed, by its key.
        }

        $data = [
            'id' => $message->id,
            'key' => $message->key,
            'subject' => $subject,
            'body' => $body,
            'viewed' => (bool)$message->viewed,
            'created_at' => $message->created_at?->timestamp,
            'has_espionage_report' => $message->espionage_report_id !== null,
            'has_battle_report' => $message->battle_report_id !== null,
        ];

        if ($full) {
            if ($message->espionage_report_id !== null) {
                $data['espionage_report'] = $this->espionageReportArray($message->espionage_report_id);
            }
            if ($message->battle_report_id !== null) {
                $data['battle_report'] = $this->battleReportArray($message->battle_report_id);
            }
        }

        return $data;
    }

    /**
     * What a spy probe found out. Which sections are filled depends on the probe count and the spy level.
     *
     * @return array<string, mixed>|null
     */
    private function espionageReportArray(int $id): array|null
    {
        $report = EspionageReport::find($id);
        if ($report === null) {
            return null;
        }

        return [
            'target' => [
                'galaxy' => $report->planet_galaxy,
                'system' => $report->planet_system,
                'position' => $report->planet_position,
                'type' => $report->planet_type,
                'owner_id' => $report->planet_user_id,
            ],
            'player_info' => $report->player_info,
            'resources' => $report->resources,
            'debris' => $report->debris,
            'buildings' => $report->buildings,
            'research' => $report->research,
            'ships' => $report->ships,
            'defense' => $report->defense,
            'counter_espionage_chance' => $report->counter_espionage_chance,
        ];
    }

    /**
     * Result of a fight. The round-by-round log is left out, only the number of rounds is given.
     *
     * @return array<string, mixed>|null
     */
    private function battleReportArray(int $id): array|null
    {
        $report = BattleReport::find($id);
        if ($report === null) {
            return null;
        }

        return [
            'target' => [
                'galaxy' => $report->planet_galaxy,
                'system' => $report->planet_system,
                'position' => $report->planet_position,
                'type' => $report->planet_type,
                'owner_id' => $report->planet_user_id,
            ],
            'general' => $report->general,
            'attacker' => $report->attacker,
            'defender' => $report->defender,
            'rounds' => is_array($report->rounds) ? count($report->rounds) : 0,
            'loot' => $report->loot,
            'debris' => $report->debris,
            'repaired_defenses' => $report->repaired_defenses,
            'wreckage' => $report->wreckage,
        ];
    }

    /**
     * Message texts are HTML in the game. Agents get plain text.
     */
    private function plainText(string $html): string
    {
        $text = preg_replace('#<br\s*/?>#i', "\n", $html) ?? $html;

        return trim(html_entity_decode(strip_tags($text), ENT_QUOTES | ENT_HTML5));
    }
}
