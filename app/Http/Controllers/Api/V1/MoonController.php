<?php

namespace OGame\Http\Controllers\Api\V1;

use Exception;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OGame\Exceptions\ApiException;
use OGame\Factories\PlanetServiceFactory;
use OGame\GameConstants\UniverseConstants;
use OGame\Models\Planet\Coordinate;
use OGame\Models\Resources;
use OGame\Services\JumpGateService;
use OGame\Services\PhalanxService;
use OGame\Services\PlayerService;

/**
 * Moon actions: sensor phalanx scan and jump gate transfer.
 *
 * Building a lunar base, sensor phalanx or jump gate needs no endpoint of its own: moons are listed by
 * GET /planets and take the normal buildings and units endpoints. This controller only wraps the two
 * actions the web UI does through ajax calls, and uses the same services (PhalanxService, JumpGateService)
 * so range, cost and cooldown rules are the game's own.
 */
class MoonController extends ApiController
{
    /**
     * Scan a planet with the sensor phalanx of this moon. Body: galaxy, system, position of the target.
     * Costs deuterium on the moon. Lists the fleets flying to (and the return flights to) the target planet.
     *
     * @throws ApiException
     */
    public function phalanx(
        Request $request,
        PlayerService $player,
        PlanetServiceFactory $planetServiceFactory,
        PhalanxService $phalanxService,
        int $planet,
    ): JsonResponse {
        $moon = $this->planetFor($player, $planet);
        $input = $this->validated($request->all(), [
            'galaxy' => ['required', 'integer', 'min:1'],
            'system' => ['required', 'integer', 'min:1', 'max:' . UniverseConstants::MAX_SYSTEM_COUNT],
            'position' => ['required', 'integer', 'min:1', 'max:' . UniverseConstants::MAX_PLANET_POSITION],
        ]);

        // Same checks, in the same order, as the web route /ajax/phalanx/scan.
        if (!$moon->isMoon()) {
            throw new ApiException('not_a_moon', 'Sensor Phalanx can only be used from a moon.', 422);
        }

        $phalanxLevel = $moon->getObjectLevel('sensor_phalanx');
        if ($phalanxLevel === 0) {
            throw new ApiException('no_sensor_phalanx', 'No Sensor Phalanx built on this moon.', 422);
        }

        $target = new Coordinate((int)$input['galaxy'], (int)$input['system'], (int)$input['position']);
        $moonCoordinates = $moon->getPlanetCoordinates();
        if (!$phalanxService->canScanTarget($moonCoordinates->galaxy, $moonCoordinates->system, $phalanxLevel, $target, $player->getId())) {
            throw new ApiException(
                'out_of_range',
                'Target is out of range. Your sensor phalanx (Level ' . $phalanxLevel . ') can scan up to '
                    . $phalanxService->calculatePhalanxRange($phalanxLevel, $player->getId()) . ' systems away.',
                422,
                ['phalanx_level' => $phalanxLevel, 'range_systems' => $phalanxService->calculatePhalanxRange($phalanxLevel, $player->getId())]
            );
        }

        if (!$phalanxService->hasEnoughDeuterium($moon->deuterium()->get())) {
            throw new ApiException('not_enough_deuterium', 'Not enough Deuterium!', 422, [
                'cost' => $phalanxService->getScanCost(),
                'available' => (int)floor($moon->deuterium()->get()),
            ]);
        }

        try {
            $targetPlanet = $planetServiceFactory->makePlanetForCoordinate($target);
        } catch (Exception) {
            $targetPlanet = null;
        }
        $targetPlayer = $targetPlanet?->getPlayer();
        if ($targetPlanet === null || $targetPlayer === null) {
            throw new ApiException('no_planet_at_target', 'No planet found at these coordinates.', 404);
        }
        if ($targetPlanet->isMoon()) {
            throw new ApiException('target_is_moon', 'Moons cannot be scanned with Sensor Phalanx.', 422);
        }
        if ($targetPlayer->isAdmin()) {
            throw new ApiException('target_is_admin', 'Administrator planets cannot be scanned.', 422);
        }
        if ($targetPlayer->getId() === $player->getId()) {
            throw new ApiException('target_is_own', 'You cannot scan your own planets.', 422);
        }

        $movements = $phalanxService->scanPlanetFleets($targetPlanet->getPlanetId(), $player->getId());

        $cost = $phalanxService->getScanCost();
        $moon->deductResources(new Resources(0, 0, $cost, 0));

        $now = time();
        $fleets = [];
        foreach ($movements as $movement) {
            $arrival = (int)$movement['display_time'];
            $fleets[] = [
                'mission_id' => $movement['mission_id'],
                'mission_type' => $movement['mission_type'],
                'mission' => $movement['mission_type_name'],
                'direction' => $movement['fleet_direction'],
                'hostile' => $movement['fleet_direction'] === 'Enemy fleet',
                'is_return_trip' => $movement['is_return_trip'],
                'owner_id' => $movement['fleet_owner_id'],
                'origin' => $movement['origin'],
                'destination' => $movement['destination'],
                'ships' => $movement['ships'],
                'ship_count' => $movement['ship_count'],
                'speed' => $movement['fleet_speed'],
                'arrival_time' => $arrival,
                'seconds_until_arrival' => max(0, $arrival - $now),
            ];
        }

        return $this->ok([
            'server_time' => $now,
            'target' => [
                'galaxy' => $target->galaxy,
                'system' => $target->system,
                'position' => $target->position,
                'planet_name' => $targetPlanet->getPlanetName(),
                'player_name' => $targetPlayer->getUsername(),
            ],
            'scan_cost' => $cost,
            'deuterium_left' => (int)floor($moon->deuterium()->get()),
            'fleet_count' => count($fleets),
            'fleets' => $fleets,
        ]);
    }

    /**
     * Send ships through the jump gate of this moon to another moon of the player.
     * Body: "target_moon_id" and "ships" ({"light_fighter": 20, ...}).
     *
     * @throws ApiException
     */
    public function jumpGate(
        Request $request,
        PlayerService $player,
        JumpGateService $jumpGateService,
        int $planet,
    ): JsonResponse {
        $moon = $this->planetFor($player, $planet);
        $transferable = $jumpGateService->getTransferableShips();
        $input = $this->validated($request->all(), [
            'target_moon_id' => ['required', 'integer', 'min:1'],
            'ships' => ['required', 'array', 'min:1'],
            'ships.*' => ['integer', 'min:1'],
        ]);
        $this->assertNotInVacationMode($player);

        // Same checks, in the same order, as the web route /ajax/jumpgate/execute.
        if (!$moon->isMoon()) {
            throw new ApiException('not_a_moon', 'Jump Gate can only be used from a moon.', 422);
        }
        if ($moon->getObjectLevel('jump_gate') < 1) {
            throw new ApiException('no_jump_gate', 'No Jump Gate built on this moon.', 422);
        }
        if ($jumpGateService->isOnCooldown($moon)) {
            throw new ApiException('jump_gate_cooldown', 'Jump Gate is not ready yet.', 422, [
                'seconds_remaining' => $jumpGateService->getRemainingCooldown($moon),
            ]);
        }

        $targetId = (int)$input['target_moon_id'];
        if (!$player->planets->planetExistsAndOwnedByPlayer($targetId)) {
            throw new ApiException('target_not_found', 'Target moon does not belong to you.', 404);
        }
        $target = $player->planets->getById($targetId);
        $target->update();
        if (!$target->isMoon()) {
            throw new ApiException('target_not_a_moon', 'Target must be a moon.', 422);
        }
        if ($target->getPlanetId() === $moon->getPlanetId()) {
            throw new ApiException('target_is_source', 'Target must be a different moon.', 422);
        }
        if ($target->getObjectLevel('jump_gate') < 1) {
            throw new ApiException('target_no_jump_gate', 'Target moon does not have a Jump Gate.', 422);
        }
        if ($jumpGateService->isOnCooldown($target)) {
            throw new ApiException('target_cooldown', 'Target Jump Gate is not ready yet.', 422, [
                'seconds_remaining' => $jumpGateService->getRemainingCooldown($target),
            ]);
        }

        $ships = [];
        foreach ($input['ships'] as $shipName => $amount) {
            if (!in_array($shipName, $transferable, true)) {
                throw new ApiException('ship_not_transferable', '"' . $shipName . '" cannot be sent through a jump gate.', 422);
            }
            $available = $moon->getObjectAmount($shipName);
            if ((int)$amount > $available) {
                throw new ApiException('not_enough_ships', 'Not enough ships on this moon: ' . $shipName . '.', 422, [
                    'ship' => $shipName,
                    'requested' => (int)$amount,
                    'available' => $available,
                ]);
            }
            $ships[$shipName] = (int)$amount;
        }

        if ($jumpGateService->hasUnprocessedArrivedFleet($moon)) {
            throw new ApiException('fleet_being_processed', 'Cannot transfer ships while a fleet mission is being processed.', 422);
        }
        if ($jumpGateService->hasUnprocessedArrivedFleet($target)) {
            throw new ApiException('target_fleet_being_processed', 'Cannot transfer ships to a moon with pending fleet mission.', 422);
        }

        if (!$jumpGateService->transferShips($moon, $target, $ships)) {
            throw new ApiException('transfer_failed', 'Failed to transfer ships. Please check your ship quantities.', 422);
        }
        $jumpGateService->setCooldown($moon, $target);

        return $this->ok([
            'from_moon_id' => $moon->getPlanetId(),
            'to_moon_id' => $target->getPlanetId(),
            'ships' => $ships,
            'cooldown_seconds' => $jumpGateService->getRemainingCooldown($moon),
        ]);
    }
}
