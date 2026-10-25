<?php

namespace Tests\Feature\Api;

use Illuminate\Testing\TestResponse;
use OGame\Models\ApiToken;
use OGame\Models\User;
use Tests\IsolatedAccountTestCase;

/**
 * Tests of the character class endpoints of the JSON API (/api/v1).
 */
class ApiV1CharacterClassTest extends IsolatedAccountTestCase
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

    private function resetClass(): void
    {
        User::where('id', $this->currentUserId)->update([
            'character_class' => null, 'character_class_free_used' => false, 'dark_matter' => 8000,
        ]);
    }

    public function testShowListsTheThreeClasses(): void
    {
        $this->resetClass();

        $response = $this->api('GET', '/api/v1/character-class')->assertOk();
        $response->assertJsonPath('data.class', null);
        $response->assertJsonPath('data.change_cost', 0);
        $this->assertSame(['collector', 'general', 'discoverer'], array_column($response->json('data.options'), 'class'));
    }

    public function testTheFirstChoiceIsFreeAndAChangeCostsDarkMatter(): void
    {
        $this->resetClass();

        $this->api('POST', '/api/v1/character-class', ['class' => 'general'])
            ->assertOk()
            ->assertJsonPath('data.class', 'general')
            ->assertJsonPath('data.dark_matter', 8000);

        // Changing now costs 500,000 dark matter, which the test player does not have.
        $this->api('POST', '/api/v1/character-class', ['class' => 'collector'])
            ->assertStatus(422)
            ->assertJsonPath('error.code', 'class_change_refused');
        $this->api('GET', '/api/v1/character-class')->assertJsonPath('data.class', 'general');
    }

    public function testUnknownClassIsRejected(): void
    {
        $this->resetClass();

        $this->api('POST', '/api/v1/character-class', ['class' => 'pirate'])->assertStatus(422);
    }
}
