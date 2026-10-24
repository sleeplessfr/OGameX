<?php

namespace OGame\Http\Controllers\Api\V1;

use Illuminate\Http\JsonResponse;
use OGame\Services\BuildingQueueService;
use OGame\Services\MessageService;
use OGame\Services\PlayerService;
use OGame\Services\ResearchQueueService;
use OGame\Services\UnitQueueService;

class StatusController extends ApiController
{
    /**
     * Who am I: the account and the IDs of its planets and moons.
     */
    public function me(PlayerService $player): JsonResponse
    {
        $planetIds = [];
        foreach ($player->planets->all() as $planet) {
            $planetIds[] = $planet->getPlanetId();
        }

        return $this->ok([
            'id' => $player->getId(),
            'username' => $player->getUsername(false),
            'email' => $player->getEmail(),
            'planet_ids' => $planetIds,
        ]);
    }

    /**
     * Everything an agent needs for one decision round, in one call: every planet with its
     * resources, production and queues, the research levels and queue, fleet slots and unread messages.
     * The data is current: production and finished queues were processed before this runs.
     */
    public function status(
        PlayerService $player,
        BuildingQueueService $buildingQueue,
        UnitQueueService $unitQueue,
        ResearchQueueService $researchQueue,
        MessageService $messages,
    ): JsonResponse {
        $planets = [];
        foreach ($player->planets->all() as $planet) {
            $planet->update();

            $planets[] = $this->planetSummary($planet)
                + $this->planetEconomy($planet)
                + [
                    'building_queue' => $this->queueArray($buildingQueue->retrieveQueue($planet)),
                    'unit_queue' => $this->queueArray($unitQueue->retrieveQueue($planet)),
                ];
        }

        return $this->ok([
            'server_time' => time(),
            'player' => [
                'id' => $player->getId(),
                'username' => $player->getUsername(false),
                'points' => $player->getCachedGeneralScore(),
                'dark_matter' => $player->getDarkMatter(),
                'vacation_mode' => $player->isInVacationMode(),
                'current_planet_id' => $player->getCurrentPlanetId(),
            ],
            'planets' => $planets,
            'research' => [
                'levels' => $player->getResearchArray(),
                'queue' => $this->queueArray($researchQueue->retrieveQueue($player->planets->current())),
            ],
            'fleet_slots' => [
                'used' => $player->getFleetSlotsInUse(),
                'max' => $player->getFleetSlotsMax(),
                'expedition_used' => $player->getExpeditionSlotsInUse(),
                'expedition_max' => $player->getExpeditionSlotsMax(),
            ],
            'unread_messages' => $messages->getUnreadMessagesCount(),
        ]);
    }
}
