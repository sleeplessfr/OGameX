<?php

namespace OGame\Http\Controllers\Api\V1;

use Illuminate\Http\JsonResponse;
use OGame\Services\BuildingQueueService;
use OGame\Services\ObjectService;
use OGame\Services\PlayerService;
use OGame\Services\UnitQueueService;

class PlanetController extends ApiController
{
    /**
     * All planets and moons of the player with their resources.
     */
    public function index(PlayerService $player): JsonResponse
    {
        $planets = [];
        foreach ($player->planets->all() as $planet) {
            $planet->update();
            $planets[] = $this->planetSummary($planet) + $this->planetEconomy($planet);
        }

        return $this->ok(['planets' => $planets]);
    }

    /**
     * One planet or moon with its building levels, ships/defense on it and its queues.
     */
    public function show(
        PlayerService $player,
        BuildingQueueService $buildingQueue,
        UnitQueueService $unitQueue,
        int $planet,
    ): JsonResponse {
        $planetService = $this->planetFor($player, $planet);

        $buildings = [];
        foreach ([...ObjectService::getBuildingObjects(), ...ObjectService::getStationObjects()] as $object) {
            if (!ObjectService::objectValidPlanetType($object->machine_name, $planetService)) {
                continue;
            }
            $level = $planetService->getObjectLevel($object->machine_name);
            if ($level > 0) {
                $buildings[$object->machine_name] = $level;
            }
        }

        $units = [];
        foreach (ObjectService::getUnitObjects() as $object) {
            $amount = $planetService->getObjectAmount($object->machine_name);
            if ($amount > 0) {
                $units[$object->machine_name] = $amount;
            }
        }

        return $this->ok(
            $this->planetSummary($planetService)
            + $this->planetEconomy($planetService)
            + [
                'buildings' => $buildings,
                'units' => $units,
                'building_queue' => $this->queueArray($buildingQueue->retrieveQueue($planetService)),
                'unit_queue' => $this->queueArray($unitQueue->retrieveQueue($planetService)),
            ]
        );
    }
}
