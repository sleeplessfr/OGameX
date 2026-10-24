<?php

namespace OGame\Http\Controllers\Api\V1;

use Exception;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OGame\Exceptions\ApiException;
use OGame\GameObjects\Models\Enums\GameObjectType;
use OGame\Services\BuildingQueueService;
use OGame\Services\ObjectService;
use OGame\Services\PlayerService;

class BuildingController extends ApiController
{
    /**
     * Every building that can be built on this planet or moon: current level, and the cost,
     * duration and conditions of the next level.
     */
    public function index(PlayerService $player, BuildingQueueService $queue, int $planet): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);
        $queueList = $queue->retrieveQueue($planetService);

        $buildings = [];
        foreach ([...ObjectService::getBuildingObjects(), ...ObjectService::getStationObjects()] as $object) {
            if (!ObjectService::objectValidPlanetType($object->machine_name, $planetService)) {
                continue;
            }

            $level = $planetService->getObjectLevel($object->machine_name);
            $price = ObjectService::getObjectPrice($object->machine_name, $planetService);

            $buildings[] = [
                'id' => $object->id,
                'machine_name' => $object->machine_name,
                'title' => $object->title,
                'level' => $level,
                'next_level' => $level + 1,
                'cost' => $this->resourcesArray($price),
                'duration_seconds' => $planetService->getBuildingConstructionTime($object->machine_name),
                'requirements_met' => ObjectService::objectRequirementsMetWithQueue($object->machine_name, $level + 1, $planetService),
                'can_afford' => $planetService->hasResources($price, $this->usesProductionEnergy($object->machine_name)),
            ];
        }

        return $this->ok([
            'planet_id' => $planetService->getPlanetId(),
            'buildings' => $buildings,
            'queue' => $this->queueArray($queueList),
            'queue_full' => $queueList->isQueueFull(),
        ]);
    }

    /**
     * Queue an upgrade of one building. Body: "machine_name" (e.g. metal_mine) or "id".
     *
     * @throws ApiException
     */
    public function build(Request $request, PlayerService $player, BuildingQueueService $queue, int $planet): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);
        $input = $this->validated($request->all(), [
            'machine_name' => ['required_without:id', 'string'],
            'id' => ['required_without:machine_name', 'integer'],
        ]);

        $this->assertNotInVacationMode($player);
        $object = $this->resolveObject($input, [GameObjectType::Building, GameObjectType::Station]);

        // Rules the web UI applies before it lets a building be queued.
        if (in_array($object->machine_name, ['shipyard', 'nano_factory'], true) && $player->isBuildingShipsOrDefense()) {
            throw new ApiException('shipyard_busy', 'The shipyard is still busy.', 422);
        }
        if ($object->machine_name === 'research_lab' && $player->isResearching()) {
            throw new ApiException('research_in_progress', 'The research lab cannot be upgraded while research is running.', 422);
        }

        // The level this upgrade would reach: the current level, or the last level already queued.
        $levelAfterQueue = $planetService->getObjectLevel($object->machine_name);
        $queueList = $queue->retrieveQueue($planetService);
        foreach ($queueList->queue as $item) {
            if ($item->object->machine_name === $object->machine_name) {
                $levelAfterQueue = $item->level_target;
            }
        }
        if (!ObjectService::objectRequirementsMetWithQueue($object->machine_name, $levelAfterQueue + 1, $planetService)) {
            throw new ApiException('requirements_not_met', 'The requirements to build this are not met.', 422);
        }

        // The queue only takes the resources when the item starts, so check them here. Otherwise an
        // order that cannot be paid would sit in the queue without ever starting.
        $price = ObjectService::getObjectPrice($object->machine_name, $planetService);
        if (!$planetService->hasResources($price, $this->usesProductionEnergy($object->machine_name))) {
            throw new ApiException('not_enough_resources', 'Not enough resources.', 422, [
                'cost' => $this->resourcesArray($price),
                'available' => $this->planetEconomy($planetService)['resources'],
            ]);
        }

        try {
            $queue->add($planetService, $object->id);
        } catch (Exception $e) {
            throw new ApiException('build_rejected', $e->getMessage(), 422);
        }

        return $this->ok(['queue' => $this->queueArray($queue->retrieveQueue($planetService))], 201);
    }

    /**
     * The building queue of a planet.
     */
    public function queue(PlayerService $player, BuildingQueueService $queue, int $planet): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);
        $queueList = $queue->retrieveQueue($planetService);

        return $this->ok([
            'queue' => $this->queueArray($queueList),
            'queue_full' => $queueList->isQueueFull(),
        ]);
    }

    /**
     * Cancel a queue item. An item that already started is refunded.
     *
     * @throws ApiException
     */
    public function cancel(PlayerService $player, BuildingQueueService $queue, int $planet, int $queueId): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);

        $item = $this->findQueueItem($queue->retrieveQueue($planetService), $queueId);
        if ($item === null) {
            throw new ApiException('queue_item_not_found', 'Queue item not found.', 404);
        }

        $queue->cancel($planetService, $queueId, $item->object->id);

        return $this->ok(['queue' => $this->queueArray($queue->retrieveQueue($planetService))]);
    }
}
