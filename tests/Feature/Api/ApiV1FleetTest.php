<?php

namespace Tests\Feature\Api;

use Illuminate\Testing\TestResponse;
use OGame\Models\ApiToken;
use OGame\Models\Resources;
use OGame\Models\User;
use Tests\IsolatedAccountTestCase;

/**
 * Tests of the fleet endpoints of the JSON API (/api/v1).
 */
class ApiV1FleetTest extends IsolatedAccountTestCase
{
    private string $apiToken = '';

    /**
     * Send a request to the API as the test player.
     *
     * @param array<string, mixed> $data
     */
    private function api(string $method, string $uri, array $data = []): TestResponse
    {
        if ($this->apiToken === '') {
            $user = User::findOrFail($this->currentUserId);
            [, $this->apiToken] = ApiToken::issue($user, 'test', 0);
        }

        return $this->withHeader('Authorization', 'Bearer ' . $this->apiToken)->json($method, $uri, $data);
    }

    /**
     * Give the planet a few ships and plenty of fuel, and return the coordinates of another player's planet.
     *
     * @return array{galaxy: int, system: int, position: int}
     */
    private function prepareFleetAndTarget(): array
    {
        $this->planetAddUnit('small_cargo', 5);
        $this->planetAddResources(new Resources(5000, 5000, 100000, 0));

        $target = $this->createForeignPlanet()->getPlanetCoordinates();

        return ['galaxy' => $target->galaxy, 'system' => $target->system, 'position' => $target->position];
    }

    /**
     * Verify that the ship list has usable numbers.
     */
    public function testShipsListHasStats(): void
    {
        $response = $this->api('GET', '/api/v1/ships')->assertOk();

        $cargo = collect($response->json('data.ships'))->firstWhere('machine_name', 'small_cargo');
        $this->assertNotNull($cargo);
        $this->assertGreaterThan(0, $cargo['speed']);
        $this->assertGreaterThan(0, $cargo['cargo_capacity']);
        $this->assertGreaterThan(0, $cargo['fuel_consumption']);
    }

    /**
     * Verify that a transport can be sent, shows up in the fleet list, and takes the ships off the planet.
     */
    public function testSendTransportAndListIt(): void
    {
        $target = $this->prepareFleetAndTarget();

        $response = $this->api('POST', "/api/v1/planets/{$this->currentPlanetId}/fleet", [
            ...$target,
            'mission' => 'transport',
            'ships' => ['small_cargo' => 2],
            'resources' => ['metal' => 100],
            'speed_percent' => 50,
        ]);
        $response->assertStatus(201)
            ->assertJsonPath('data.mission.mission', 'transport')
            ->assertJsonPath('data.mission.is_mine', true)
            ->assertJsonPath('data.mission.ships.small_cargo', 2)
            ->assertJsonPath('data.mission.resources.metal', 100)
            ->assertJsonPath('data.mission.can_recall', true);
        $this->assertGreaterThan(0, $response->json('data.mission.seconds_remaining'));

        $list = $this->api('GET', '/api/v1/fleet')->assertOk();
        $this->assertSame(1, $list->json('data.fleet_slots.used'));
        $this->assertCount(1, $list->json('data.missions'));

        $units = $this->api('GET', "/api/v1/planets/{$this->currentPlanetId}/units")->assertOk();
        $cargo = collect($units->json('data.units'))->firstWhere('machine_name', 'small_cargo');
        $this->assertSame(3, $cargo['amount']);
    }

    /**
     * Verify that a flying fleet can be recalled once, and not twice.
     */
    public function testRecallFleet(): void
    {
        $target = $this->prepareFleetAndTarget();
        $missionId = (int)$this->api('POST', "/api/v1/planets/{$this->currentPlanetId}/fleet", [
            ...$target,
            'mission' => 'transport',
            'ships' => ['small_cargo' => 1],
        ])->assertStatus(201)->json('data.mission.id');

        $this->api('POST', "/api/v1/fleet/{$missionId}/recall")
            ->assertOk()
            ->assertJsonPath('data.recalled_mission_id', $missionId);

        $this->api('POST', "/api/v1/fleet/{$missionId}/recall")
            ->assertStatus(422)
            ->assertJsonPath('error.code', 'not_recallable');

        $this->api('POST', '/api/v1/fleet/999999999/recall')
            ->assertStatus(404)
            ->assertJsonPath('error.code', 'mission_not_found');
    }

    /**
     * Verify the reasons a fleet order is refused.
     */
    public function testSendRefusals(): void
    {
        $target = $this->prepareFleetAndTarget();
        $url = "/api/v1/planets/{$this->currentPlanetId}/fleet";
        $order = [...$target, 'mission' => 'transport', 'ships' => ['small_cargo' => 1]];

        // The game's own reason: not enough ships on the planet.
        $this->api('POST', $url, [...$order, 'ships' => ['small_cargo' => 50]])
            ->assertStatus(422)->assertJsonPath('error.code', 'mission_rejected');

        $this->api('POST', $url, [...$order, 'ships' => ['rocket_launcher' => 1]])
            ->assertStatus(422)->assertJsonPath('error.code', 'unknown_ship');
        $this->api('POST', $url, [...$order, 'ships' => ['small_cargo' => 0]])
            ->assertStatus(422)->assertJsonPath('error.code', 'validation_failed');
        $this->api('POST', $url, [...$order, 'mission' => 'dance'])
            ->assertStatus(422)->assertJsonPath('error.code', 'validation_failed');
        $this->api('POST', $url, [...$order, 'galaxy' => 99])
            ->assertStatus(422)->assertJsonPath('error.code', 'invalid_coordinates');
        $this->api('POST', $url, [...$order, 'speed_percent' => 12])
            ->assertStatus(422)->assertJsonPath('error.code', 'invalid_speed');
        $this->api('POST', $url, [...$order, 'mission' => 'expedition', 'position' => 16])
            ->assertStatus(422)->assertJsonPath('error.code', 'invalid_holding_time');

        // Nothing was sent by any of the refused orders.
        $this->assertSame(0, $this->api('GET', '/api/v1/fleet')->json('data.fleet_slots.used'));
    }

    /**
     * Verify that other players' planets cannot be used to send a fleet.
     */
    public function testCannotSendFromAnotherPlayersPlanet(): void
    {
        $target = $this->prepareFleetAndTarget();
        $foreignId = $this->createForeignPlanet()->getPlanetId();

        $this->api('POST', "/api/v1/planets/{$foreignId}/fleet", [
            ...$target,
            'mission' => 'transport',
            'ships' => ['small_cargo' => 1],
        ])->assertStatus(404)->assertJsonPath('error.code', 'planet_not_found');
    }

    /**
     * Verify the dry run: which missions are possible, the flight time and the fuel cost, without sending anything.
     */
    public function testCheckDoesNotSendButReportsWhatIsPossible(): void
    {
        $target = $this->prepareFleetAndTarget();

        $response = $this->api('POST', "/api/v1/planets/{$this->currentPlanetId}/fleet/check", [
            ...$target,
            'ships' => ['small_cargo' => 2],
        ])->assertOk();

        $this->assertTrue($response->json('data.missions.transport.possible'));
        $this->assertFalse($response->json('data.missions.colonisation.possible'));
        $this->assertNotEmpty($response->json('data.missions.colonisation.reason'));
        $this->assertGreaterThan(0, $response->json('data.duration_seconds'));
        $this->assertGreaterThan(0, $response->json('data.deuterium_consumption'));
        $this->assertGreaterThan(0, $response->json('data.cargo_capacity'));

        $this->assertSame(0, $this->api('GET', '/api/v1/fleet')->json('data.fleet_slots.used'));
    }
}
