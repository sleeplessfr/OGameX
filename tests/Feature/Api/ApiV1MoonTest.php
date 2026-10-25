<?php

namespace Tests\Feature\Api;

use Illuminate\Testing\TestResponse;
use OGame\Factories\PlanetServiceFactory;
use OGame\Models\ApiToken;
use OGame\Models\Resources;
use OGame\Models\User;
use OGame\Services\ObjectService;
use OGame\Services\PlanetService;
use Tests\MoonTestCase;

/**
 * Tests of the moon endpoints of the JSON API (/api/v1): listing and building on a moon,
 * sensor phalanx scan and jump gate transfer.
 */
class ApiV1MoonTest extends MoonTestCase
{
    private string $apiToken = '';

    /**
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

    private function setLevel(PlanetService $planet, string $machineName, int $level): void
    {
        $planet->setObjectLevel(ObjectService::getObjectByMachineName($machineName)->id, $level, true);
        $planet->reloadPlanet();
    }

    /**
     * @return array<string, int>
     */
    private function targetBody(PlanetService $target): array
    {
        $coords = $target->getPlanetCoordinates();

        return ['galaxy' => $coords->galaxy, 'system' => $coords->system, 'position' => $coords->position];
    }

    public function testMoonIsListedAndTakesTheBuildingEndpoints(): void
    {
        $moonId = $this->moonService->getPlanetId();

        $planets = $this->api('GET', '/api/v1/planets')->assertOk()->json('data.planets');
        $types = [];
        foreach ($planets as $planet) {
            $types[$planet['id']] = $planet['type'];
        }
        $this->assertSame('moon', $types[$moonId]);

        $response = $this->api('GET', '/api/v1/planets/' . $moonId . '/buildings')->assertOk();
        $names = array_column($response->json('data.buildings'), 'machine_name');
        $this->assertContains('lunar_base', $names);
        $this->assertContains('sensor_phalanx', $names);
        $this->assertContains('jump_gate', $names);
        $this->assertNotContains('metal_mine', $names);
    }

    public function testPhalanxNeedsAMoonWithAPhalanx(): void
    {
        $target = $this->createForeignPlanet();
        $this->moonService->addResources(new Resources(0, 0, 10000, 0));

        $this->api('POST', '/api/v1/planets/' . $this->planetService->getPlanetId() . '/phalanx', $this->targetBody($target))
            ->assertStatus(422)->assertJsonPath('error.code', 'not_a_moon');
        $this->api('POST', '/api/v1/planets/' . $this->moonService->getPlanetId() . '/phalanx', $this->targetBody($target))
            ->assertStatus(422)->assertJsonPath('error.code', 'no_sensor_phalanx');
    }

    public function testPhalanxChecksDeuteriumAndTarget(): void
    {
        $this->setLevel($this->moonService, 'sensor_phalanx', 5);
        $target = $this->createForeignPlanet();
        $moonUrl = '/api/v1/planets/' . $this->moonService->getPlanetId() . '/phalanx';

        $this->api('POST', $moonUrl, $this->targetBody($target))
            ->assertStatus(422)->assertJsonPath('error.code', 'not_enough_deuterium');

        $this->moonService->addResources(new Resources(0, 0, 10000, 0));
        $this->api('POST', $moonUrl, ['galaxy' => 1])->assertStatus(422)->assertJsonPath('error.code', 'validation_failed');
        $this->api('POST', $moonUrl, $this->targetBody($this->planetService))
            ->assertStatus(422)->assertJsonPath('error.code', 'target_is_own');
    }

    public function testPhalanxOutOfRange(): void
    {
        // Level 1 reaches only the own system.
        $this->setLevel($this->moonService, 'sensor_phalanx', 1);
        $this->moonService->addResources(new Resources(0, 0, 10000, 0));
        $coords = $this->moonService->getPlanetCoordinates();
        $farSystem = $coords->system > 250 ? $coords->system - 100 : $coords->system + 100;

        $this->api('POST', '/api/v1/planets/' . $this->moonService->getPlanetId() . '/phalanx', [
            'galaxy' => $coords->galaxy, 'system' => $farSystem, 'position' => 5,
        ])->assertStatus(422)->assertJsonPath('error.code', 'out_of_range');
    }

    public function testPhalanxScanCostsDeuterium(): void
    {
        $this->setLevel($this->moonService, 'sensor_phalanx', 5);
        $this->moonService->addResources(new Resources(0, 0, 10000, 0));
        $target = $this->createForeignPlanet();
        $before = $this->moonService->deuterium()->get();

        $this->api('POST', '/api/v1/planets/' . $this->moonService->getPlanetId() . '/phalanx', $this->targetBody($target))
            ->assertOk()
            ->assertJsonPath('data.scan_cost', 5000)
            ->assertJsonPath('data.fleet_count', 0)
            ->assertJsonPath('data.fleets', []);

        $this->moonService->reloadPlanet();
        $this->assertEquals($before - 5000, $this->moonService->deuterium()->get());
    }

    public function testJumpGateMovesShipsAndSetsCooldown(): void
    {
        $secondMoon = resolve(PlanetServiceFactory::class)->createMoonForPlanet($this->secondPlanetService, 100000, 20);
        $this->setLevel($this->moonService, 'jump_gate', 1);
        $this->setLevel($secondMoon, 'jump_gate', 1);
        $this->moonService->addUnit('small_cargo', 10, true);
        $this->moonService->reloadPlanet();
        $url = '/api/v1/planets/' . $this->moonService->getPlanetId() . '/jump-gate';

        $this->api('POST', $url, ['target_moon_id' => $secondMoon->getPlanetId(), 'ships' => ['small_cargo' => 50]])
            ->assertStatus(422)->assertJsonPath('error.code', 'not_enough_ships');
        $this->api('POST', $url, ['target_moon_id' => $secondMoon->getPlanetId(), 'ships' => ['solar_satellite' => 1]])
            ->assertStatus(422)->assertJsonPath('error.code', 'ship_not_transferable');
        $this->api('POST', $url, ['target_moon_id' => $this->planetService->getPlanetId(), 'ships' => ['small_cargo' => 1]])
            ->assertStatus(422)->assertJsonPath('error.code', 'target_not_a_moon');

        $this->api('POST', $url, ['target_moon_id' => $secondMoon->getPlanetId(), 'ships' => ['small_cargo' => 4]])
            ->assertOk()
            ->assertJsonPath('data.ships.small_cargo', 4);

        $this->moonService->reloadPlanet();
        $secondMoon->reloadPlanet();
        $this->assertEquals(6, $this->moonService->getObjectAmount('small_cargo'));
        $this->assertEquals(4, $secondMoon->getObjectAmount('small_cargo'));

        // Both gates are cooling down now.
        $this->api('POST', $url, ['target_moon_id' => $secondMoon->getPlanetId(), 'ships' => ['small_cargo' => 1]])
            ->assertStatus(422)->assertJsonPath('error.code', 'jump_gate_cooldown');
    }

    public function testJumpGateNeedsAGate(): void
    {
        $secondMoon = resolve(PlanetServiceFactory::class)->createMoonForPlanet($this->secondPlanetService, 100000, 20);

        $this->api('POST', '/api/v1/planets/' . $this->moonService->getPlanetId() . '/jump-gate', [
            'target_moon_id' => $secondMoon->getPlanetId(), 'ships' => ['small_cargo' => 1],
        ])->assertStatus(422)->assertJsonPath('error.code', 'no_jump_gate');
    }
}
