<?php

namespace Tests\Feature\Api;

use Illuminate\Support\Facades\Cache;
use Illuminate\Testing\TestResponse;
use OGame\Models\ApiToken;
use OGame\Models\Resources;
use OGame\Models\User;
use OGame\Services\MerchantService;
use Tests\IsolatedAccountTestCase;

/**
 * Tests of the merchant endpoints of the JSON API (/api/v1).
 */
class ApiV1MerchantTest extends IsolatedAccountTestCase
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

    private function setDarkMatter(int $amount): void
    {
        User::where('id', $this->currentUserId)->update(['dark_matter' => $amount]);
    }

    /**
     * Resources of the test planet as the API reports them.
     *
     * @return array<string, int>
     */
    private function resources(): array
    {
        $planets = $this->api('GET', '/api/v1/status')->json('data.planets');

        return $planets[0]['resources'];
    }

    public function testShowWithoutMerchant(): void
    {
        $this->setDarkMatter(8000);
        Cache::forget('active_merchant_' . $this->currentUserId);

        $this->api('GET', '/api/v1/merchant')
            ->assertOk()
            ->assertJsonPath('data.dark_matter', 8000)
            ->assertJsonPath('data.call_cost', MerchantService::DARK_MATTER_COST)
            ->assertJsonPath('data.merchant', null);
    }

    public function testCallCostsDarkMatterAndShowsRates(): void
    {
        $this->setDarkMatter(8000);
        Cache::forget('active_merchant_' . $this->currentUserId);

        $response = $this->api('POST', '/api/v1/merchant/call', ['type' => 'metal'])->assertStatus(201);
        $response->assertJsonPath('data.dark_matter', 8000 - MerchantService::DARK_MATTER_COST);
        $response->assertJsonPath('data.merchant.type', 'metal');
        $rates = $response->json('data.merchant.receive');
        $this->assertEqualsCanonicalizing(['crystal', 'deuterium'], array_keys($rates));
        $this->assertGreaterThan(0, $rates['crystal']['per_unit_given']);
        // The rates stay available to later GET requests.
        $this->api('GET', '/api/v1/merchant')->assertJsonPath('data.merchant.type', 'metal');
    }

    public function testCallRefusals(): void
    {
        $this->setDarkMatter(MerchantService::DARK_MATTER_COST - 1);
        Cache::forget('active_merchant_' . $this->currentUserId);

        $this->api('POST', '/api/v1/merchant/call', [])->assertStatus(422)->assertJsonPath('error.code', 'validation_failed');
        $this->api('POST', '/api/v1/merchant/call', ['type' => 'gold'])->assertStatus(422)->assertJsonPath('error.code', 'validation_failed');
        $this->api('POST', '/api/v1/merchant/call', ['type' => 'metal'])->assertStatus(422)->assertJsonPath('error.code', 'insufficient_dark_matter');

        // A waiting merchant is not replaced (that would waste the dark matter already paid).
        $this->setDarkMatter(20000);
        $this->api('POST', '/api/v1/merchant/call', ['type' => 'metal'])->assertStatus(201);
        $this->api('POST', '/api/v1/merchant/call', ['type' => 'crystal'])->assertStatus(422)->assertJsonPath('error.code', 'merchant_already_active');
    }

    public function testTradeMovesResourcesAndTheMerchantLeaves(): void
    {
        $this->setDarkMatter(8000);
        Cache::forget('active_merchant_' . $this->currentUserId);
        $this->planetAddResources(new Resources(5000, 0, 0, 0));
        $before = $this->resources();

        $this->api('POST', '/api/v1/merchant/call', ['type' => 'metal'])->assertStatus(201);
        $response = $this->api('POST', "/api/v1/planets/{$this->currentPlanetId}/merchant/trade", [
            'receive' => ['crystal' => 1000],
            'give_amount' => 4000,
        ])->assertOk();

        $gave = $response->json('data.gave.metal');
        $this->assertGreaterThan(0, $gave);
        $this->assertLessThanOrEqual(4000, $gave);
        $this->assertSame(1000, $response->json('data.received.crystal'));

        $after = $this->resources();
        $this->assertLessThan($before['metal'], $after['metal']);
        $this->assertGreaterThanOrEqual($before['crystal'] + 1000, $after['crystal']);

        // One trade only: the merchant is gone.
        $this->api('GET', '/api/v1/merchant')->assertJsonPath('data.merchant', null);
        $this->api('POST', "/api/v1/planets/{$this->currentPlanetId}/merchant/trade", [
            'receive' => ['crystal' => 10], 'give_amount' => 100,
        ])->assertStatus(422)->assertJsonPath('error.code', 'no_active_merchant');
    }

    public function testTradeRefusals(): void
    {
        $this->setDarkMatter(8000);
        Cache::forget('active_merchant_' . $this->currentUserId);
        $url = "/api/v1/planets/{$this->currentPlanetId}/merchant/trade";

        $this->api('POST', $url, [])->assertStatus(422)->assertJsonPath('error.code', 'validation_failed');

        $this->api('POST', '/api/v1/merchant/call', ['type' => 'metal'])->assertStatus(201);
        // He buys metal, so he cannot pay in metal.
        $this->api('POST', $url, ['receive' => ['metal' => 100], 'give_amount' => 100])
            ->assertStatus(422)->assertJsonPath('error.code', 'validation_failed');
        // Still waiting after a refused trade.
        $this->api('GET', '/api/v1/merchant')->assertJsonPath('data.merchant.type', 'metal');
    }

    public function testDismissAndOtherPlayersPlanets(): void
    {
        $this->setDarkMatter(8000);
        $this->api('POST', '/api/v1/merchant/call', ['type' => 'deuterium'])->assertStatus(201);
        $this->api('POST', '/api/v1/merchant/dismiss')->assertOk()->assertJsonPath('data.merchant', null);
        // The dark matter is not refunded.
        $this->api('GET', '/api/v1/merchant')->assertJsonPath('data.dark_matter', 8000 - MerchantService::DARK_MATTER_COST);

        $foreignPlanetId = $this->createForeignPlanet()->getPlanetId();
        $this->api('POST', "/api/v1/planets/{$foreignPlanetId}/merchant/trade", ['receive' => ['crystal' => 1], 'give_amount' => 1])
            ->assertStatus(404);
    }
}
