<?php

namespace OGame\Http\Controllers\Api\V1;

use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OGame\Exceptions\ApiException;
use OGame\GameObjects\Models\Enums\GameObjectType;
use OGame\Services\ObjectService;
use OGame\Services\PlayerService;
use OGame\Services\UnitQueueService;

/**
 * Ships and defense units, built on a planet or moon.
 */
class UnitController extends ApiController
{
    /**
     * Every ship and defense unit: how many are on this planet, cost and build time per unit,
     * and how many could be ordered right now.
     */
    public function index(PlayerService $player, UnitQueueService $queue, int $planet): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);

        $units = [];
        foreach (ObjectService::getUnitObjects() as $object) {
            $requirementsMet = ObjectService::objectRequirementsMet($object->machine_name, $planetService);
            $classMet = ObjectService::objectCharacterClassMet($object->machine_name, $planetService);

            $units[] = [
                'id' => $object->id,
                'machine_name' => $object->machine_name,
                'title' => $object->title,
                'category' => $object->type->value,
                'amount' => $planetService->getObjectAmount($object->machine_name),
                'cost_per_unit' => $this->resourcesArray(ObjectService::getObjectPrice($object->machine_name, $planetService)),
                'seconds_per_unit' => $planetService->getUnitConstructionTime($object->machine_name),
                'requirements_met' => $requirementsMet,
                'character_class_met' => $classMet,
                'max_buildable' => ObjectService::getObjectMaxBuildAmount($object->machine_name, $planetService, $requirementsMet && $classMet),
            ];
        }

        return $this->ok([
            'planet_id' => $planetService->getPlanetId(),
            'units' => $units,
            'queue' => $this->queueArray($queue->retrieveQueue($planetService)),
        ]);
    }

    /**
     * Order ships or defense units. Body: "machine_name" (e.g. small_cargo) or "id", and "amount".
     *
     * If the planet cannot pay for the whole amount, as many units as it can afford are ordered
     * (the web UI does the same). The response says how many were ordered.
     *
     * @throws ApiException
     */
    public function build(Request $request, PlayerService $player, UnitQueueService $queue, int $planet): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);
        $input = $this->validated($request->all(), [
            'machine_name' => ['required_without:id', 'string'],
            'id' => ['required_without:machine_name', 'integer'],
            'amount' => ['required', 'integer', 'min:1', 'max:1000000'],
        ]);

        $this->assertNotInVacationMode($player);
        $object = $this->resolveObject($input, [GameObjectType::Ship, GameObjectType::Defense]);

        // The unit queue silently ignores orders it cannot take, so check everything up front and
        // answer with a clear reason instead of a success that did nothing.
        if (!ObjectService::objectRequirementsMet($object->machine_name, $planetService)) {
            throw new ApiException('requirements_not_met', 'The requirements to build this are not met.', 422);
        }
        if (!ObjectService::objectCharacterClassMet($object->machine_name, $planetService)) {
            throw new ApiException('character_class_required', 'This unit needs a different character class.', 422);
        }

        $requested = (int)$input['amount'];
        $maxBuildable = ObjectService::getObjectMaxBuildAmount($object->machine_name, $planetService, true);
        if ($maxBuildable < 1) {
            throw new ApiException('not_enough_resources', 'Not enough resources to build even one.', 422, [
                'cost_per_unit' => $this->resourcesArray(ObjectService::getObjectPrice($object->machine_name, $planetService)),
                'available' => $this->planetEconomy($planetService)['resources'],
            ]);
        }

        $ordered = min($requested, $maxBuildable);
        $queue->add($planetService, $object->id, $ordered);

        return $this->ok([
            'requested' => $requested,
            'ordered' => $ordered,
            'queue' => $this->queueArray($queue->retrieveQueue($planetService)),
        ], 201);
    }

    /**
     * The ship and defense queue of a planet. Unit orders cannot be cancelled.
     */
    public function queue(PlayerService $player, UnitQueueService $queue, int $planet): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);

        return $this->ok(['queue' => $this->queueArray($queue->retrieveQueue($planetService))]);
    }
}
