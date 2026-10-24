<?php

namespace OGame\Http\Controllers\Api\V1;

use Illuminate\Http\JsonResponse;
use OGame\GameConstants\UniverseConstants;
use OGame\Services\SettingsService;

class UniverseController extends ApiController
{
    /**
     * Public description of the universe: its size, speeds and the server clock.
     */
    public function show(SettingsService $settings): JsonResponse
    {
        return $this->ok([
            'name' => $settings->universeName(),
            'galaxies' => $settings->numberOfGalaxies(),
            'systems_per_galaxy' => UniverseConstants::MAX_SYSTEM_COUNT,
            // Positions 1..15 hold planets. Position 16 is reserved for expeditions.
            'planet_positions' => UniverseConstants::MAX_PLANET_POSITION,
            'expedition_position' => UniverseConstants::EXPEDITION_POSITION,
            'speed' => [
                'economy' => $settings->economySpeed(),
                'research' => $settings->researchSpeed(),
                'fleet' => $settings->fleetSpeed(),
            ],
            'server_time' => time(),
        ]);
    }
}
