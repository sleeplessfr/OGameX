<?php

namespace OGame\Http\Controllers\Api\V1;

use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OGame\Exceptions\ApiException;
use OGame\Models\Highscore;
use OGame\Services\HighscoreService;
use OGame\Services\PlayerService;

/**
 * The ranking of players, and where the calling player stands.
 */
class HighscoreController extends ApiController
{
    /** API type => HighscoreTypeEnum value used by HighscoreService::setHighscoreType(). */
    private const TYPES = ['general' => 0, 'economy' => 1, 'research' => 2, 'military' => 3];

    /**
     * Query: type (general|economy|research|military, default general), page, per_page (1-100, default 25).
     * The game caches the ranking for a few minutes, so it can lag behind a little.
     *
     * @throws ApiException
     */
    public function index(Request $request, PlayerService $player, HighscoreService $highscores): JsonResponse
    {
        $input = $this->validated($request->query(), [
            'type' => ['nullable', 'string', 'in:' . implode(',', array_keys(self::TYPES))],
            'page' => ['nullable', 'integer', 'min:1'],
            'per_page' => ['nullable', 'integer', 'min:1', 'max:100'],
        ]);
        $type = $input['type'] ?? 'general';
        $page = (int)($input['page'] ?? 1);
        $perPage = (int)($input['per_page'] ?? 25);

        $highscores->setHighscoreType(self::TYPES[$type]);

        $players = [];
        foreach ($highscores->getHighscorePlayers($perPage, $page) as $row) {
            $coordinates = $row['planet_coords'];
            $players[] = [
                'rank' => (int)$row['rank'],
                'user_id' => (int)$row['id'],
                'username' => $row['name'],
                'score' => (int)$row['points'],
                'is_me' => (int)$row['id'] === $player->getId(),
                'main_planet' => [
                    'galaxy' => $coordinates->galaxy,
                    'system' => $coordinates->system,
                    'position' => $coordinates->position,
                ],
                'alliance_tag' => $row['alliance_tag'],
                'total_ships' => $row['total_ships'],
            ];
        }

        $mine = Highscore::where('player_id', $player->getId())->first();
        $total = $highscores->getHighscorePlayerAmount();

        return $this->ok([
            'type' => $type,
            'page' => $page,
            'per_page' => $perPage,
            'total_players' => $total,
            'total_pages' => max(1, (int)ceil($total / $perPage)),
            'players' => $players,
            'me' => [
                'general' => ['rank' => (int)($mine?->general_rank ?? 0), 'score' => (int)($mine?->general ?? 0)],
                'economy' => ['rank' => (int)($mine?->economy_rank ?? 0), 'score' => (int)($mine?->economy ?? 0)],
                'research' => ['rank' => (int)($mine?->research_rank ?? 0), 'score' => (int)($mine?->research ?? 0)],
                'military' => ['rank' => (int)($mine?->military_rank ?? 0), 'score' => (int)($mine?->military ?? 0)],
            ],
        ]);
    }
}
