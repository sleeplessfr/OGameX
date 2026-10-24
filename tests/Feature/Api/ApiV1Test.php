<?php

namespace Tests\Feature\Api;

use Illuminate\Support\Str;
use Illuminate\Testing\TestResponse;
use OGame\Models\ApiToken;
use OGame\Models\BuildingQueue;
use OGame\Models\Resources;
use OGame\Models\User;
use Tests\IsolatedAccountTestCase;

/**
 * Tests of the JSON API (/api/v1) used by bots and AI agents.
 */
class ApiV1Test extends IsolatedAccountTestCase
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
     * Verify that the universe description is public and matches the game's constants.
     */
    public function testUniverseIsPublic(): void
    {
        $this->getJson('/api/v1/universe')
            ->assertOk()
            ->assertJsonPath('success', true)
            ->assertJsonPath('data.systems_per_galaxy', 499)
            ->assertJsonPath('data.planet_positions', 15)
            ->assertJsonPath('data.expedition_position', 16);
    }

    /**
     * Verify that endpoints refuse requests without a valid token.
     */
    public function testProtectedEndpointsNeedAValidToken(): void
    {
        $this->getJson('/api/v1/status')
            ->assertStatus(401)
            ->assertJsonPath('success', false)
            ->assertJsonPath('error.code', 'unauthenticated');

        $this->withHeader('Authorization', 'Bearer ogx_doesnotexist')
            ->getJson('/api/v1/status')
            ->assertStatus(401)
            ->assertJsonPath('error.code', 'invalid_token');
    }

    /**
     * Verify the account flow: register, log in, use the token, log out.
     */
    public function testRegisterLoginAndLogout(): void
    {
        $email = 'agent_' . Str::random(10) . '@example.com';
        $password = 'Str0ng-' . Str::random(16);

        $register = $this->postJson('/api/v1/auth/register', [
            'email' => $email,
            'password' => $password,
            'token_name' => 'test-agent',
        ]);
        $register->assertStatus(201)
            ->assertJsonPath('success', true)
            ->assertJsonPath('data.token_type', 'Bearer')
            ->assertJsonPath('data.user.email', $email);
        $this->assertStringStartsWith('ogx_', (string)$register->json('data.token'));

        $this->postJson('/api/v1/auth/login', ['email' => $email, 'password' => 'not-the-password'])
            ->assertStatus(401)
            ->assertJsonPath('error.code', 'invalid_credentials');

        $login = $this->postJson('/api/v1/auth/login', ['email' => $email, 'password' => $password]);
        $login->assertOk();
        $token = (string)$login->json('data.token');

        $this->withHeader('Authorization', 'Bearer ' . $token)
            ->getJson('/api/v1/me')
            ->assertOk()
            ->assertJsonPath('data.email', $email);

        $this->withHeader('Authorization', 'Bearer ' . $token)->postJson('/api/v1/auth/logout')->assertOk();

        $this->withHeader('Authorization', 'Bearer ' . $token)->getJson('/api/v1/me')->assertStatus(401);
    }

    /**
     * Verify that registering with an e-mail address that is already used is refused.
     */
    public function testRegisterRejectsDuplicateEmail(): void
    {
        $email = User::findOrFail($this->currentUserId)->email;

        $this->postJson('/api/v1/auth/register', ['email' => $email, 'password' => 'Str0ng-' . Str::random(16)])
            ->assertStatus(422)
            ->assertJsonPath('error.code', 'validation_failed');
    }

    /**
     * Verify that the status call returns every planet, the research levels and the fleet slots.
     */
    public function testStatusShowsPlanetsResearchAndFleetSlots(): void
    {
        $response = $this->api('GET', '/api/v1/status');

        $response->assertOk()->assertJsonPath('data.player.id', $this->currentUserId);
        $this->assertCount(2, $response->json('data.planets'));
        $this->assertIsInt($response->json('data.planets.0.resources.metal'));
        $this->assertArrayHasKey('computer_technology', $response->json('data.research.levels'));
        $this->assertGreaterThan(0, $response->json('data.fleet_slots.max'));
    }

    /**
     * Verify that a building can be queued and cancelled.
     */
    public function testBuildAndCancelMetalMine(): void
    {
        $this->planetAddResources(new Resources(1000, 1000, 1000, 0));
        $planetId = $this->currentPlanetId;

        $response = $this->api('POST', "/api/v1/planets/{$planetId}/buildings", ['machine_name' => 'metal_mine']);
        $response->assertStatus(201)
            ->assertJsonPath('data.queue.0.machine_name', 'metal_mine')
            ->assertJsonPath('data.queue.0.target_level', 1)
            ->assertJsonPath('data.queue.0.active', true);
        $queueId = (int)$response->json('data.queue.0.queue_id');

        $this->assertSame(1, BuildingQueue::where([['planet_id', $planetId], ['canceled', 0], ['processed', 0]])->count());

        $this->api('DELETE', "/api/v1/planets/{$planetId}/buildings/queue/{$queueId}")
            ->assertOk()
            ->assertJsonCount(0, 'data.queue');

        $this->assertSame(0, BuildingQueue::where([['planet_id', $planetId], ['canceled', 0], ['processed', 0]])->count());
    }

    /**
     * Verify the reasons a building order is refused.
     */
    public function testBuildRefusals(): void
    {
        $planetId = $this->currentPlanetId;
        $url = "/api/v1/planets/{$planetId}/buildings";

        $this->api('POST', $url, [])->assertStatus(422)->assertJsonPath('error.code', 'validation_failed');
        $this->api('POST', $url, ['machine_name' => 'not_a_building'])->assertStatus(422)->assertJsonPath('error.code', 'unknown_object');
        $this->api('POST', $url, ['machine_name' => 'small_cargo'])->assertStatus(422)->assertJsonPath('error.code', 'wrong_object_type');
        $this->api('POST', $url, ['machine_name' => 'fusion_plant'])->assertStatus(422)->assertJsonPath('error.code', 'requirements_not_met');

        // Spend everything, then even a metal mine cannot be paid.
        $this->planetDeductResources(new Resources(
            (int)floor($this->planetService->metal()->get()),
            (int)floor($this->planetService->crystal()->get()),
            (int)floor($this->planetService->deuterium()->get()),
            0
        ));
        $this->api('POST', $url, ['machine_name' => 'metal_mine'])
            ->assertStatus(422)
            ->assertJsonPath('error.code', 'not_enough_resources');
    }

    /**
     * Verify that planets of other players are not reachable, whatever the endpoint.
     */
    public function testOtherPlayersPlanetsAreNotFound(): void
    {
        $foreignId = $this->createForeignPlanet()->getPlanetId();

        $this->api('GET', "/api/v1/planets/{$foreignId}")->assertStatus(404)->assertJsonPath('error.code', 'planet_not_found');
        $this->api('GET', "/api/v1/planets/{$foreignId}/buildings")->assertStatus(404);
        $this->api('POST', "/api/v1/planets/{$foreignId}/buildings", ['machine_name' => 'metal_mine'])->assertStatus(404);
        $this->api('POST', "/api/v1/planets/{$foreignId}/units", ['machine_name' => 'rocket_launcher', 'amount' => 1])->assertStatus(404);
        $this->api('POST', "/api/v1/planets/{$foreignId}/research", ['machine_name' => 'energy_technology'])->assertStatus(404);
        $this->api('GET', '/api/v1/planets/999999999')->assertStatus(404);
    }

    /**
     * Verify that research can be started and shows up in the research queue.
     */
    public function testStartResearch(): void
    {
        $this->planetSetObjectLevel('research_lab', 1);
        $this->planetAddResources(new Resources(5000, 5000, 5000, 0));

        $this->api('POST', "/api/v1/planets/{$this->currentPlanetId}/research", ['machine_name' => 'energy_technology'])
            ->assertStatus(201)
            ->assertJsonPath('data.queue.0.machine_name', 'energy_technology')
            ->assertJsonPath('data.queue.0.target_level', 1);

        $this->api('GET', '/api/v1/research/queue')
            ->assertOk()
            ->assertJsonCount(1, 'data.queue');
    }

    /**
     * Verify that defense units can be ordered, and that an order larger than the planet can
     * pay for is reduced to what it can afford.
     */
    public function testOrderUnits(): void
    {
        $this->planetSetObjectLevel('shipyard', 1);
        $this->planetAddResources(new Resources(10000, 10000, 10000, 0));
        $url = "/api/v1/planets/{$this->currentPlanetId}/units";

        $this->api('POST', $url, ['machine_name' => 'rocket_launcher', 'amount' => 3])
            ->assertStatus(201)
            ->assertJsonPath('data.requested', 3)
            ->assertJsonPath('data.ordered', 3)
            ->assertJsonPath('data.queue.0.machine_name', 'rocket_launcher')
            ->assertJsonPath('data.queue.0.amount', 3);

        $tooMany = $this->api('POST', $url, ['machine_name' => 'rocket_launcher', 'amount' => 1000000]);
        $tooMany->assertStatus(201);
        $this->assertSame(1000000, $tooMany->json('data.requested'));
        $this->assertGreaterThanOrEqual(1, $tooMany->json('data.ordered'));
        $this->assertLessThan(1000000, $tooMany->json('data.ordered'));
    }

    /**
     * Verify the galaxy view: 15 positions, with the player's own planet marked.
     */
    public function testGalaxyView(): void
    {
        $coordinates = $this->planetService->getPlanetCoordinates();

        $response = $this->api('GET', "/api/v1/galaxy/{$coordinates->galaxy}/{$coordinates->system}");
        $response->assertOk();

        $positions = $response->json('data.positions');
        $this->assertCount(15, $positions);
        $mine = $positions[$coordinates->position - 1];
        $this->assertSame($coordinates->position, $mine['position']);
        $this->assertTrue($mine['planet']['owner']['is_me']);

        $this->api('GET', '/api/v1/galaxy/99/1')->assertStatus(422)->assertJsonPath('error.code', 'invalid_coordinates');
        $this->api('GET', '/api/v1/galaxy/1/500')->assertStatus(422)->assertJsonPath('error.code', 'invalid_coordinates');
    }
}
