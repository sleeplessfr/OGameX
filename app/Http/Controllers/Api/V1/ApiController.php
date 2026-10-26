<?php

namespace OGame\Http\Controllers\Api\V1;

use Illuminate\Http\JsonResponse;
use Illuminate\Routing\Controller;
use Illuminate\Support\Facades\Validator;
use OGame\Exceptions\ApiException;
use OGame\GameObjects\Models\Abstracts\GameObject;
use OGame\GameObjects\Models\Enums\GameObjectType;
use OGame\Models\Resources;
use OGame\Services\ObjectService;
use OGame\Services\PlanetService;
use OGame\Services\PlayerService;
use OGame\ViewModels\Queue\Abstracts\QueueViewModel;
use OGame\ViewModels\Queue\BuildingQueueListViewModel;
use OGame\ViewModels\Queue\BuildingQueueViewModel;
use OGame\ViewModels\Queue\ResearchQueueListViewModel;
use OGame\ViewModels\Queue\ResearchQueueViewModel;
use OGame\ViewModels\Queue\UnitQueueListViewModel;
use OGame\ViewModels\Queue\UnitQueueViewModel;
use RuntimeException;

/**
 * Shared helpers of the JSON API controllers.
 *
 * Controllers never use a constructor: Laravel creates a controller before the route middleware
 * has run, and the services we need depend on the player that "globalgame" loads. All services
 * are therefore injected into the action methods instead.
 */
abstract class ApiController extends Controller
{
    /**
     * Success response: {"success": true, "data": ...}
     */
    protected function ok(mixed $data, int $status = 200): JsonResponse
    {
        return response()->json(['success' => true, 'data' => $data], $status);
    }

    /**
     * Validate input, throwing an ApiException (422) that lists every problem.
     *
     * @param array<string, mixed> $input
     * @param array<string, mixed> $rules
     * @return array<string, mixed> The validated input.
     * @throws ApiException
     */
    protected function validated(array $input, array $rules): array
    {
        $validator = Validator::make($input, $rules);
        if ($validator->fails()) {
            throw new ApiException('validation_failed', 'The given data was invalid.', 422, $validator->errors()->toArray());
        }

        return $validator->validated();
    }

    /**
     * Get one of the player's own planets or moons, brought up to date. Anything else is a 404,
     * the same answer for "does not exist" and "belongs to someone else".
     *
     * @throws ApiException
     */
    protected function planetFor(PlayerService $player, int $planetId): PlanetService
    {
        if (!$player->planets->planetExistsAndOwnedByPlayer($planetId)) {
            throw new ApiException('planet_not_found', 'Planet not found.', 404);
        }

        $planet = $player->planets->getById($planetId);
        $planet->updateIfStale();

        return $planet;
    }

    /**
     * Players in vacation mode cannot start anything.
     *
     * @throws ApiException
     */
    protected function assertNotInVacationMode(PlayerService $player): void
    {
        if ($player->isInVacationMode()) {
            throw new ApiException('vacation_mode', 'The account is in vacation mode.', 422);
        }
    }

    /**
     * Find a game object from a request that has "machine_name" or "id".
     *
     * @param array<string, mixed> $input Validated input.
     * @param array<int, GameObjectType> $allowedTypes
     * @throws ApiException
     */
    protected function resolveObject(array $input, array $allowedTypes): GameObject
    {
        try {
            if (isset($input['machine_name'])) {
                $object = ObjectService::getObjectByMachineName((string)$input['machine_name']);
            } else {
                $object = ObjectService::getObjectById((int)$input['id']);
            }
        } catch (RuntimeException) {
            throw new ApiException('unknown_object', 'There is no game object with that machine_name or id.', 422);
        }

        if (!in_array($object->type, $allowedTypes, true)) {
            throw new ApiException(
                'wrong_object_type',
                '"' . $object->machine_name . '" is a ' . $object->type->value . ' and cannot be used with this endpoint.',
                422
            );
        }

        return $object;
    }

    /**
     * Whether paying for this object also needs the planet's energy (the web UI applies this rule).
     */
    protected function usesProductionEnergy(string $machineName): bool
    {
        return in_array($machineName, ['terraformer', 'space_dock'], true);
    }

    /**
     * @return array{metal: int, crystal: int, deuterium: int, energy: int}
     */
    protected function resourcesArray(Resources $resources): array
    {
        return [
            'metal' => (int)floor($resources->metal->get()),
            'crystal' => (int)floor($resources->crystal->get()),
            'deuterium' => (int)floor($resources->deuterium->get()),
            'energy' => (int)floor($resources->energy->get()),
        ];
    }

    /**
     * Identity of a planet or moon.
     *
     * @return array<string, mixed>
     */
    protected function planetSummary(PlanetService $planet): array
    {
        $coordinates = $planet->getPlanetCoordinates();

        return [
            'id' => $planet->getPlanetId(),
            'name' => $planet->getPlanetName(),
            'type' => $planet->isMoon() ? 'moon' : 'planet',
            'coordinates' => [
                'galaxy' => $coordinates->galaxy,
                'system' => $coordinates->system,
                'position' => $coordinates->position,
            ],
            'fields' => [
                'used' => $planet->getBuildingCount(),
                'max' => $planet->getPlanetFieldMax(),
            ],
        ];
    }

    /**
     * Stock, storage capacity, hourly production and energy balance of a planet.
     *
     * @return array<string, mixed>
     */
    protected function planetEconomy(PlanetService $planet): array
    {
        return [
            'resources' => [
                'metal' => (int)floor($planet->metal()->get()),
                'crystal' => (int)floor($planet->crystal()->get()),
                'deuterium' => (int)floor($planet->deuterium()->get()),
            ],
            'storage' => [
                'metal' => (int)floor($planet->metalStorage()->get()),
                'crystal' => (int)floor($planet->crystalStorage()->get()),
                'deuterium' => (int)floor($planet->deuteriumStorage()->get()),
            ],
            'production_per_hour' => [
                'metal' => (int)floor($planet->getMetalProductionPerHour()),
                'crystal' => (int)floor($planet->getCrystalProductionPerHour()),
                'deuterium' => (int)floor($planet->getDeuteriumProductionPerHour()),
            ],
            'energy' => (int)floor($planet->energy()->get()),
        ];
    }

    /**
     * Queue as a list: the item being worked on first, then the waiting items.
     *
     * @return array<int, array<string, mixed>>
     */
    protected function queueArray(BuildingQueueListViewModel|ResearchQueueListViewModel|UnitQueueListViewModel $queue): array
    {
        $items = [];

        $active = $queue->getCurrentlyBuildingFromQueue();
        if ($active !== null) {
            $items[] = $this->queueItemArray($active, true);
        }
        foreach ($queue->getQueuedFromQueue() as $item) {
            $items[] = $this->queueItemArray($item, false);
        }

        return $items;
    }

    /**
     * Find a queue item (active or waiting) by its queue ID.
     */
    protected function findQueueItem(BuildingQueueListViewModel|ResearchQueueListViewModel|UnitQueueListViewModel $queue, int $queueId): QueueViewModel|null
    {
        $active = $queue->getCurrentlyBuildingFromQueue();
        if ($active !== null && $active->id === $queueId) {
            return $active;
        }
        foreach ($queue->getQueuedFromQueue() as $item) {
            if ($item->id === $queueId) {
                return $item;
            }
        }

        return null;
    }

    /**
     * @return array<string, mixed>
     */
    private function queueItemArray(QueueViewModel $item, bool $active): array
    {
        // Both time values are 0 while an item is still waiting for its turn.
        $data = [
            'queue_id' => $item->id,
            'object_id' => $item->object->id,
            'machine_name' => $item->object->machine_name,
            'title' => $item->object->title,
            'active' => $active,
            'seconds_remaining' => $item->time_countdown,
            'duration_seconds' => $item->time_total,
        ];

        if ($item instanceof BuildingQueueViewModel) {
            $data['target_level'] = $item->level_target;
            $data['is_downgrade'] = $item->is_downgrade;
        } elseif ($item instanceof ResearchQueueViewModel) {
            $data['target_level'] = $item->level_target;
            $data['planet_id'] = $item->planet->getPlanetId();
        } elseif ($item instanceof UnitQueueViewModel) {
            $data['amount'] = $item->object_amount;
            $data['amount_remaining'] = $item->object_amount_remaining;
            $data['seconds_until_next_unit'] = $item->time_countdown_object_next;
            $data['seconds_per_unit'] = $item->time_countdown_per_object;
        }

        return $data;
    }
}
