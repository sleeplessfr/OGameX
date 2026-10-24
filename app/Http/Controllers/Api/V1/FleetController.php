<?php

namespace OGame\Http\Controllers\Api\V1;

use Exception;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Date;
use OGame\Exceptions\ApiException;
use OGame\Factories\GameMissionFactory;
use OGame\GameConstants\UniverseConstants;
use OGame\GameObjects\Models\Units\UnitCollection;
use OGame\Models\Enums\PlanetType;
use OGame\Models\FleetMission;
use OGame\Models\Planet\Coordinate;
use OGame\Models\Resources;
use OGame\Services\FleetMissionService;
use OGame\Services\ObjectService;
use OGame\Services\PlanetService;
use OGame\Services\PlayerService;
use OGame\Services\SettingsService;

/**
 * Fleets: list them, check and send a mission, recall a fleet.
 *
 * Sending goes through the same game code as the web UI (FleetMissionService::createNewFromPlanet),
 * so fuel, cargo, fleet slots, mission rules and the attack block are exactly the game's own.
 * Alliance combat (ACS), missiles and fleet unions are not part of this API yet.
 */
class FleetController extends ApiController
{
    /** Mission names accepted by the API, and their game mission type ids. */
    private const MISSIONS = [
        'attack' => 1,
        'transport' => 3,
        'deployment' => 4,
        'espionage' => 6,
        'colonisation' => 7,
        'recycle' => 8,
        'moon_destruction' => 9,
        'expedition' => 15,
    ];

    /** Every mission type id the game knows, for describing fleets that are already flying. */
    private const MISSION_LABELS = [
        1 => 'attack', 2 => 'acs_attack', 3 => 'transport', 4 => 'deployment', 5 => 'acs_defend',
        6 => 'espionage', 7 => 'colonisation', 8 => 'recycle', 9 => 'moon_destruction', 10 => 'missile_attack',
        15 => 'expedition',
    ];

    private const TARGET_TYPES = [
        'planet' => PlanetType::Planet,
        'moon' => PlanetType::Moon,
        'debris_field' => PlanetType::DebrisField,
        'deep_space' => PlanetType::DeepSpace,
    ];

    /**
     * Stats of every ship, with the player's research already applied (speed, cargo, fuel).
     */
    public function ships(PlayerService $player): JsonResponse
    {
        $ships = [];
        foreach (ObjectService::getShipObjects() as $ship) {
            $ships[] = [
                'id' => $ship->id,
                'machine_name' => $ship->machine_name,
                'title' => $ship->title,
                'speed' => (int)$ship->properties->speed->calculate($player)->totalValue,
                'cargo_capacity' => (int)$ship->properties->capacity->calculate($player)->totalValue,
                'fuel_capacity' => (int)$ship->properties->fuel_capacity->calculate($player)->totalValue,
                'fuel_consumption' => (int)$ship->properties->fuel->calculate($player)->totalValue,
                'attack' => (int)$ship->properties->attack->calculate($player)->totalValue,
                'shield' => (int)$ship->properties->shield->calculate($player)->totalValue,
                'structural_integrity' => (int)$ship->properties->structural_integrity->calculate($player)->totalValue,
            ];
        }

        return $this->ok(['ships' => $ships]);
    }

    /**
     * The player's fleets that are flying (outgoing, returning), and fleets of other players
     * that are heading for one of the player's planets (without their composition).
     */
    public function index(PlayerService $player, FleetMissionService $fleets): JsonResponse
    {
        $missions = [];
        foreach ($fleets->getActiveFleetMissionsForCurrentPlayer() as $mission) {
            $missions[] = $this->missionArray($mission, $player, $fleets);
        }

        return $this->ok([
            'fleet_slots' => [
                'used' => $player->getFleetSlotsInUse(),
                'max' => $player->getFleetSlotsMax(),
                'expedition_used' => $player->getExpeditionSlotsInUse(),
                'expedition_max' => $player->getExpeditionSlotsMax(),
            ],
            'missions' => $missions,
        ]);
    }

    /**
     * Ask what a fleet could do without sending it: which missions are possible, how long the
     * flight takes and how much deuterium it uses. Same body as sending.
     *
     * @throws ApiException
     */
    public function check(Request $request, PlayerService $player, FleetMissionService $fleets, SettingsService $settings, int $planet): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);
        $order = $this->parseOrder($request, $player, $settings, $planetService, missionRequired: false);

        $possible = [];
        foreach (GameMissionFactory::getAllMissions() as $mission) {
            $name = self::MISSION_LABELS[$mission::getTypeId()] ?? null;
            if ($name === null || !isset(self::MISSIONS[$name])) {
                continue; // ACS and missiles are not available through the API
            }
            $status = $mission->isMissionPossible($planetService, $order['target'], $order['type'], $order['units']);
            // The game sometimes refuses without saying why, so always give a reason when it is not possible.
            $reason = $status->error !== '' ? $status->error : 'This mission is not possible with this fleet and target.';
            $possible[$name] = ['possible' => $status->possible, 'reason' => $status->possible ? null : $reason];
        }

        $duration = $fleets->calculateFleetMissionDuration($planetService, $order['target'], $order['units'], null, $order['speed']);
        $consumption = (int)ceil($fleets->calculateConsumption($planetService, $order['units'], $order['target'], $order['holding_hours'], $order['speed']));

        return $this->ok([
            'missions' => $possible,
            'duration_seconds' => $duration,
            'deuterium_consumption' => $consumption,
            'cargo_capacity' => $order['units']->getTotalCargoCapacity($player),
            'fuel_capacity' => $order['units']->getTotalFuelCapacity($player),
            'distance' => $fleets->calculateFleetMissionDistance($planetService, $order['target']),
        ]);
    }

    /**
     * Send a fleet from one of the player's planets or moons.
     *
     * Body: mission, galaxy, system, position, ships {machine_name: amount}, and optionally
     * type (planet|moon|debris_field), resources {metal, crystal, deuterium}, speed_percent (10-100, steps
     * of 5, default 100) and holding_hours (expeditions: 1 to your astrophysics level).
     *
     * @throws ApiException
     */
    public function send(Request $request, PlayerService $player, FleetMissionService $fleets, SettingsService $settings, int $planet): JsonResponse
    {
        $planetService = $this->planetFor($player, $planet);
        $this->assertNotInVacationMode($player);
        $order = $this->parseOrder($request, $player, $settings, $planetService, missionRequired: true);

        if ($settings->missionBlockedByAttackBlock($order['mission_id'])) {
            throw new ApiException('attack_block', 'The attack block is active. Only friendly fleets can be started right now.', 422);
        }

        try {
            $mission = $fleets->createNewFromPlanet(
                $planetService,
                $order['target'],
                $order['type'],
                $order['mission_id'],
                $order['units'],
                $order['resources'],
                $order['speed'],
                $order['holding_hours'],
            );
        } catch (Exception $e) {
            // The game's own reason: no free fleet slot, not enough fuel or cargo, mission not possible here, ...
            throw new ApiException('mission_rejected', $e->getMessage(), 422);
        }

        return $this->ok(['mission' => $this->missionArray($mission, $player, $fleets)], 201);
    }

    /**
     * Call a flying fleet back. It turns around and flies home.
     *
     * @throws ApiException
     */
    public function recall(PlayerService $player, FleetMissionService $fleets, int $mission): JsonResponse
    {
        $fleetMission = $fleets->getFleetMissionById($mission, false);
        if ($fleetMission === null || $fleetMission->user_id !== $player->getId()) {
            throw new ApiException('mission_not_found', 'Fleet mission not found.', 404);
        }
        if (!$this->canRecall($fleetMission)) {
            throw new ApiException('not_recallable', 'This fleet cannot be recalled (already arrived, returning or cancelled).', 422);
        }

        $fleets->cancelMission($fleetMission);

        // cancelMission() quietly does nothing when the game refuses, so check that it really worked.
        $fresh = $fleets->getFleetMissionById($mission, false);
        if ($fresh === null || !$fresh->canceled) {
            throw new ApiException('not_recallable', 'The game did not accept the recall.', 422);
        }

        return $this->ok(['recalled_mission_id' => $mission]);
    }

    /**
     * Read and validate the body shared by "check" and "send".
     *
     * @return array{mission_id: int, target: Coordinate, type: PlanetType, units: UnitCollection, resources: Resources, speed: float, holding_hours: int}
     * @throws ApiException
     */
    private function parseOrder(Request $request, PlayerService $player, SettingsService $settings, PlanetService $planet, bool $missionRequired): array
    {
        $input = $this->validated($request->all(), [
            'mission' => [$missionRequired ? 'required' : 'nullable', 'string', 'in:' . implode(',', array_keys(self::MISSIONS))],
            'galaxy' => ['required', 'integer'],
            'system' => ['required', 'integer'],
            'position' => ['required', 'integer'],
            'type' => ['nullable', 'string', 'in:' . implode(',', array_keys(self::TARGET_TYPES))],
            'ships' => ['required', 'array', 'min:1'],
            'ships.*' => ['integer', 'min:0', 'max:1000000000'],
            'resources' => ['nullable', 'array'],
            'resources.metal' => ['nullable', 'integer', 'min:0'],
            'resources.crystal' => ['nullable', 'integer', 'min:0'],
            'resources.deuterium' => ['nullable', 'integer', 'min:0'],
            'speed_percent' => ['nullable', 'integer', 'min:10', 'max:100'],
            'holding_hours' => ['nullable', 'integer', 'min:0', 'max:1000'],
        ]);

        $galaxy = (int)$input['galaxy'];
        $system = (int)$input['system'];
        $position = (int)$input['position'];
        if ($galaxy < UniverseConstants::MIN_GALAXY || $galaxy > $settings->numberOfGalaxies()
            || $system < UniverseConstants::MIN_SYSTEM || $system > UniverseConstants::MAX_SYSTEM_COUNT
            || $position < UniverseConstants::MIN_PLANET_POSITION || $position > UniverseConstants::EXPEDITION_POSITION) {
            throw new ApiException('invalid_coordinates', 'Coordinates are outside the universe.', 422);
        }

        $speedPercent = (int)($input['speed_percent'] ?? 100);
        if ($speedPercent % 5 !== 0) {
            throw new ApiException('invalid_speed', 'speed_percent must be a multiple of 5 between 10 and 100.', 422);
        }

        $units = new UnitCollection();
        $shipsByName = [];
        foreach (ObjectService::getShipObjects() as $ship) {
            $shipsByName[$ship->machine_name] = $ship;
        }
        foreach ($input['ships'] as $machineName => $amount) {
            if (!isset($shipsByName[$machineName])) {
                throw new ApiException('unknown_ship', '"' . $machineName . '" is not a ship.', 422);
            }
            if ((int)$amount > 0) {
                $units->addUnit($shipsByName[$machineName], (int)$amount);
            }
        }
        if ($units->getAmount() < 1) {
            throw new ApiException('validation_failed', 'The fleet needs at least one ship.', 422, ['ships' => ['Send at least one ship.']]);
        }

        $missionName = $input['mission'] ?? null;
        $missionId = $missionName !== null ? self::MISSIONS[$missionName] : 0;
        $holdingHours = (int)($input['holding_hours'] ?? 0);
        if ($missionName === 'expedition') {
            $maxHours = $player->getResearchLevel('astrophysics');
            if ($holdingHours < 1 || $holdingHours > $maxHours) {
                throw new ApiException('invalid_holding_time', 'holding_hours for an expedition must be between 1 and ' . $maxHours . ' (your astrophysics level).', 422);
            }
        }

        $type = self::TARGET_TYPES[$input['type'] ?? 'planet'];
        $resources = new Resources(
            (int)($input['resources']['metal'] ?? 0),
            (int)($input['resources']['crystal'] ?? 0),
            (int)($input['resources']['deuterium'] ?? 0),
            0
        );

        return [
            'mission_id' => $missionId,
            'target' => new Coordinate($galaxy, $system, $position),
            'type' => $type,
            // The game counts speed in tenths: 10 = 100%.
            'speed' => $speedPercent / 10,
            'units' => $units,
            'resources' => $resources,
            'holding_hours' => $holdingHours,
        ];
    }

    private function canRecall(FleetMission $mission): bool
    {
        if ($mission->canceled || $mission->processed) {
            return false;
        }
        // A return trip cannot be recalled again, and neither can moving a planet's ships to itself.
        if (!empty($mission->parent_id)) {
            return false;
        }
        if ($mission->mission_type === 4 && $mission->planet_id_from === $mission->planet_id_to) {
            return false;
        }

        return $mission->time_arrival >= Date::now()->timestamp;
    }

    /**
     * @return array<string, mixed>
     */
    private function missionArray(FleetMission $mission, PlayerService $player, FleetMissionService $fleets): array
    {
        $mine = $mission->user_id === $player->getId();
        $now = Date::now()->timestamp;

        $data = [
            'id' => $mission->id,
            'mission' => self::MISSION_LABELS[$mission->mission_type] ?? ('type_' . $mission->mission_type),
            'mission_type' => $mission->mission_type,
            'is_mine' => $mine,
            'is_return' => !empty($mission->parent_id),
            'from' => [
                'planet_id' => $mission->planet_id_from,
                'type' => PlanetType::tryFrom((int)$mission->type_from)?->name,
                'galaxy' => $mission->galaxy_from,
                'system' => $mission->system_from,
                'position' => $mission->position_from,
            ],
            'to' => [
                'planet_id' => $mission->planet_id_to,
                'type' => PlanetType::tryFrom((int)$mission->type_to)?->name,
                'galaxy' => $mission->galaxy_to,
                'system' => $mission->system_to,
                'position' => $mission->position_to,
            ],
            'departure_at' => $mission->time_departure,
            'arrival_at' => $mission->time_arrival,
            'seconds_remaining' => max(0, $mission->time_arrival - $now),
        ];

        // Only the owner sees what is on board. Other players' fleets show where they come from and when they arrive.
        if ($mine) {
            $ships = [];
            foreach ($fleets->getFleetUnits($mission)->units as $entry) {
                $ships[$entry->unitObject->machine_name] = $entry->amount;
            }
            $data['ships'] = $ships;
            $data['resources'] = [
                'metal' => (int)$mission->metal,
                'crystal' => (int)$mission->crystal,
                'deuterium' => (int)$mission->deuterium,
            ];
            $data['can_recall'] = $this->canRecall($mission);
        }

        return $data;
    }
}
