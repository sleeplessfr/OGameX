<?php

namespace OGame\Http\Controllers\Api\V1;

use Exception;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OGame\Exceptions\ApiException;
use OGame\GameObjects\Models\Enums\GameObjectType;
use OGame\Services\ObjectService;
use OGame\Services\PlayerService;
use OGame\Services\ResearchQueueService;

/**
 * Research belongs to the player, but it is paid with the resources of one planet and timed by
 * that planet's research lab, so the endpoints that start or list research take a planet.
 */
class ResearchController extends ApiController
{
    /**
     * Every technology with its level and the cost and duration of the next level on this planet.
     */
    public function index(PlayerService $player, ResearchQueueService $queue, int $planet): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);
        $queueList = $queue->retrieveQueue($planetService);

        $research = [];
        foreach (ObjectService::getResearchObjects() as $object) {
            $level = $player->getResearchLevel($object->machine_name);
            $price = ObjectService::getObjectPrice($object->machine_name, $planetService);

            $research[] = [
                'id' => $object->id,
                'machine_name' => $object->machine_name,
                'title' => $object->title,
                'level' => $level,
                'next_level' => $level + 1,
                'cost' => $this->resourcesArray($price),
                'duration_seconds' => (int)ceil($planetService->getTechnologyResearchTime($object->machine_name)),
                'requirements_met' => ObjectService::objectRequirementsMetWithQueue($object->machine_name, $level + 1, $planetService),
                'can_afford' => $planetService->hasResources($price),
            ];
        }

        return $this->ok([
            'planet_id' => $planetService->getPlanetId(),
            'research' => $research,
            'queue' => $this->queueArray($queueList),
            'queue_full' => $queueList->isQueueFull(),
            'research_lab_upgrading' => $player->isBuildingObject('research_lab'),
        ]);
    }

    /**
     * Queue a technology, paid by the given planet. Body: "machine_name" (e.g. energy_technology) or "id".
     *
     * @throws ApiException
     */
    public function start(Request $request, PlayerService $player, ResearchQueueService $queue, int $planet): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);
        $input = $this->validated($request->all(), [
            'machine_name' => ['required_without:id', 'string'],
            'id' => ['required_without:machine_name', 'integer'],
        ]);

        $this->assertNotInVacationMode($player);
        $object = $this->resolveObject($input, [GameObjectType::Research]);

        // Rule the web UI applies: no research while the research lab is being upgraded.
        if ($player->isBuildingObject('research_lab')) {
            throw new ApiException('research_lab_upgrading', 'Cannot research while the research lab is being upgraded.', 422);
        }

        // The level this order would reach: the current level plus the levels already queued.
        $nextLevel = $player->getResearchLevel($object->machine_name)
            + $queue->activeResearchQueueItemCount($player, $object->id)
            + 1;
        if (!ObjectService::objectRequirementsMetWithQueue($object->machine_name, $nextLevel, $planetService)) {
            throw new ApiException('requirements_not_met', 'The requirements to research this are not met.', 422);
        }

        // The queue only takes the resources when the item starts, so check them here. Otherwise an
        // order that cannot be paid would sit in the queue without ever starting.
        $price = ObjectService::getObjectPrice($object->machine_name, $planetService);
        if (!$planetService->hasResources($price)) {
            throw new ApiException('not_enough_resources', 'Not enough resources.', 422, [
                'cost' => $this->resourcesArray($price),
                'available' => $this->planetEconomy($planetService)['resources'],
            ]);
        }

        try {
            $queue->add($player, $planetService, $object->id);
        } catch (Exception $e) {
            throw new ApiException('research_rejected', $e->getMessage(), 422);
        }

        return $this->ok(['queue' => $this->queueArray($queue->retrieveQueue($planetService))], 201);
    }

    /**
     * The research queue of the player (all planets).
     */
    public function queue(PlayerService $player, ResearchQueueService $queue): JsonResponse
    {
        $queueList = $queue->retrieveQueue($player->planets->current());

        return $this->ok([
            'queue' => $this->queueArray($queueList),
            'queue_full' => $queueList->isQueueFull(),
        ]);
    }

    /**
     * Cancel a research queue item. An item that already started is refunded.
     *
     * @throws ApiException
     */
    public function cancel(PlayerService $player, ResearchQueueService $queue, int $queueId): JsonResponse
    {
        $item = $this->findQueueItem($queue->retrieveQueue($player->planets->current()), $queueId);
        if ($item === null) {
            throw new ApiException('queue_item_not_found', 'Queue item not found.', 404);
        }

        $queue->cancel($player, $queueId, $item->object->id);

        return $this->ok(['queue' => $this->queueArray($queue->retrieveQueue($player->planets->current()))]);
    }
}
