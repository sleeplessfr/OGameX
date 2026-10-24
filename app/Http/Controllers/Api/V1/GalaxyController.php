<?php

namespace OGame\Http\Controllers\Api\V1;

use Illuminate\Http\JsonResponse;
use OGame\Exceptions\ApiException;
use OGame\GameConstants\UniverseConstants;
use OGame\Models\Enums\PlanetType;
use OGame\Models\Planet;
use OGame\Models\User;
use OGame\Services\PlayerService;
use OGame\Services\SettingsService;

class GalaxyController extends ApiController
{
    /**
     * One solar system: for each of the 15 planet positions, either null (empty) or the planet
     * that is there, who owns it and whether it has a moon.
     *
     * @throws ApiException
     */
    public function show(PlayerService $player, SettingsService $settings, int $galaxy, int $system): JsonResponse
    {
        if ($galaxy < UniverseConstants::MIN_GALAXY || $galaxy > $settings->numberOfGalaxies()) {
            throw new ApiException('invalid_coordinates', 'Galaxy must be between ' . UniverseConstants::MIN_GALAXY . ' and ' . $settings->numberOfGalaxies() . '.', 422);
        }
        if ($system < UniverseConstants::MIN_SYSTEM || $system > UniverseConstants::MAX_SYSTEM_COUNT) {
            throw new ApiException('invalid_coordinates', 'System must be between ' . UniverseConstants::MIN_SYSTEM . ' and ' . UniverseConstants::MAX_SYSTEM_COUNT . '.', 422);
        }

        $rows = Planet::where('galaxy', $galaxy)
            ->where('system', $system)
            ->where('destroyed', 0)
            ->whereIn('planet_type', [PlanetType::Planet->value, PlanetType::Moon->value])
            ->get();

        $usernames = User::whereIn('id', $rows->pluck('user_id')->unique()->all())->pluck('username', 'id');

        $planetsByPosition = [];
        $positionsWithMoon = [];
        foreach ($rows as $row) {
            if ((int)$row->planet_type === PlanetType::Moon->value) {
                $positionsWithMoon[(int)$row->planet] = true;
            } else {
                $planetsByPosition[(int)$row->planet] = $row;
            }
        }

        $positions = [];
        for ($position = UniverseConstants::MIN_PLANET_POSITION; $position <= UniverseConstants::MAX_PLANET_POSITION; $position++) {
            $row = $planetsByPosition[$position] ?? null;
            if ($row === null) {
                $positions[] = ['position' => $position, 'planet' => null];
                continue;
            }

            $positions[] = [
                'position' => $position,
                'planet' => [
                    'id' => (int)$row->id,
                    'name' => $row->name,
                    'owner' => [
                        'id' => (int)$row->user_id,
                        'username' => $usernames[(int)$row->user_id] ?? null,
                        'is_me' => (int)$row->user_id === $player->getId(),
                    ],
                    'has_moon' => isset($positionsWithMoon[$position]),
                ],
            ];
        }

        return $this->ok([
            'galaxy' => $galaxy,
            'system' => $system,
            'positions' => $positions,
        ]);
    }
}
