<?php

namespace Tests\Feature\Api;

use Illuminate\Testing\TestResponse;
use OGame\Models\ApiToken;
use OGame\Models\EspionageReport;
use OGame\Models\Message;
use OGame\Models\User;
use Tests\IsolatedAccountTestCase;

/**
 * Tests of the message and highscore endpoints of the JSON API (/api/v1).
 */
class ApiV1MessagesTest extends IsolatedAccountTestCase
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

    private function createWelcomeMessage(int $userId): Message
    {
        $message = new Message();
        $message->user_id = $userId;
        $message->key = 'welcome_message';
        $message->params = ['player' => '[player]' . $userId . '[/player]'];
        $message->save();

        return $message;
    }

    private function createSpyReportMessage(int $userId): Message
    {
        $report = new EspionageReport();
        $report->planet_galaxy = 1;
        $report->planet_system = 2;
        $report->planet_position = 3;
        $report->planet_type = 1;
        $report->planet_user_id = $userId;
        $report->player_info = ['player_id' => (string)$userId, 'player_name' => 'Somebody'];
        $report->resources = ['metal' => 12345, 'crystal' => 678, 'deuterium' => 9, 'energy' => 0];
        $report->buildings = ['metal_mine' => 7];
        $report->ships = ['small_cargo' => 4];
        $report->save();

        $message = new Message();
        $message->user_id = $userId;
        $message->key = 'espionage_report';
        $message->espionage_report_id = $report->id;
        $message->save();

        return $message;
    }

    /**
     * Verify listing, filtering by unread and marking a message as read.
     */
    public function testListAndMarkRead(): void
    {
        $message = $this->createWelcomeMessage($this->currentUserId);

        $list = $this->api('GET', '/api/v1/messages')->assertOk();
        $row = collect($list->json('data.messages'))->firstWhere('id', $message->id);
        $this->assertNotNull($row);
        $this->assertFalse($row['viewed']);
        $this->assertSame('welcome_message', $row['key']);

        // Listing alone does not mark anything as read.
        $unread = $this->api('GET', '/api/v1/messages?unread_only=1')->assertOk();
        $this->assertNotNull(collect($unread->json('data.messages'))->firstWhere('id', $message->id));

        $this->api('POST', "/api/v1/messages/{$message->id}/read")
            ->assertOk()
            ->assertJsonPath('data.message.viewed', true);

        $unread = $this->api('GET', '/api/v1/messages?unread_only=1')->assertOk();
        $this->assertNull(collect($unread->json('data.messages'))->firstWhere('id', $message->id));
    }

    /**
     * Verify that a spy report comes back as structured data, and is found through its category.
     */
    public function testSpyReportIsStructured(): void
    {
        $message = $this->createSpyReportMessage($this->currentUserId);

        $this->api('GET', "/api/v1/messages/{$message->id}")
            ->assertOk()
            ->assertJsonPath('data.message.has_espionage_report', true)
            ->assertJsonPath('data.message.espionage_report.target.galaxy', 1)
            ->assertJsonPath('data.message.espionage_report.target.system', 2)
            ->assertJsonPath('data.message.espionage_report.target.position', 3)
            ->assertJsonPath('data.message.espionage_report.resources.metal', 12345)
            ->assertJsonPath('data.message.espionage_report.buildings.metal_mine', 7)
            ->assertJsonPath('data.message.espionage_report.ships.small_cargo', 4);

        $byCategory = $this->api('GET', '/api/v1/messages?category=espionage')->assertOk();
        $this->assertNotNull(collect($byCategory->json('data.messages'))->firstWhere('id', $message->id));

        // The list itself stays light: no report data, only the flag.
        $row = collect($byCategory->json('data.messages'))->firstWhere('id', $message->id);
        $this->assertTrue($row['has_espionage_report']);
        $this->assertArrayNotHasKey('espionage_report', $row);
    }

    /**
     * Verify that other players' messages are not reachable.
     */
    public function testOtherPlayersMessagesAreNotFound(): void
    {
        $foreignId = $this->createForeignPlanet()->getPlayer()?->getId();
        $this->assertNotNull($foreignId);
        $foreign = $this->createSpyReportMessage($foreignId);

        $this->api('GET', "/api/v1/messages/{$foreign->id}")->assertStatus(404)->assertJsonPath('error.code', 'message_not_found');
        $this->api('POST', "/api/v1/messages/{$foreign->id}/read")->assertStatus(404);

        $list = $this->api('GET', '/api/v1/messages?per_page=50')->assertOk();
        $this->assertNull(collect($list->json('data.messages'))->firstWhere('id', $foreign->id));
        $this->assertSame(0, Message::where('id', $foreign->id)->where('viewed', 1)->count());
    }

    /**
     * Verify input checks on the message list.
     */
    public function testMessageListValidation(): void
    {
        $this->api('GET', '/api/v1/messages?category=nonsense')->assertStatus(422)->assertJsonPath('error.code', 'validation_failed');
        $this->api('GET', '/api/v1/messages?per_page=500')->assertStatus(422);
        $this->api('GET', '/api/v1/messages/999999999')->assertStatus(404);
    }

    /**
     * Verify the highscore answer has the expected shape, including where the player stands.
     */
    public function testHighscoreShape(): void
    {
        $response = $this->api('GET', '/api/v1/highscore?type=economy&per_page=10')->assertOk();

        $response->assertJsonPath('data.type', 'economy');
        $this->assertIsArray($response->json('data.players'));
        $this->assertIsInt($response->json('data.total_players'));
        foreach (['general', 'economy', 'research', 'military'] as $type) {
            $this->assertIsInt($response->json("data.me.{$type}.rank"));
            $this->assertIsInt($response->json("data.me.{$type}.score"));
        }

        $this->api('GET', '/api/v1/highscore?type=nonsense')->assertStatus(422);
        $this->api('GET', '/api/v1/highscore')->assertOk()->assertJsonPath('data.type', 'general');
    }
}
