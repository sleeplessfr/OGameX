<?php

namespace Tests\Feature\Api;

use Illuminate\Testing\TestResponse;
use OGame\Models\Alliance;
use OGame\Models\ApiToken;
use OGame\Models\Message;
use OGame\Models\User;
use OGame\Services\AllianceService;
use Tests\IsolatedAccountTestCase;

/**
 * Tests of the alliance and chat endpoints of the JSON API (/api/v1).
 */
class ApiV1AllianceTest extends IsolatedAccountTestCase
{
    /** @var array<int, string> API token per user id. */
    private array $tokens = [];

    /**
     * Send a request to the API as the test player, or as another user.
     *
     * @param array<string, mixed> $data
     */
    private function api(string $method, string $uri, array $data = [], int|null $userId = null): TestResponse
    {
        $userId ??= $this->currentUserId;
        if (!isset($this->tokens[$userId])) {
            [, $this->tokens[$userId]] = ApiToken::issue(User::findOrFail($userId), 'test', 0);
        }

        return $this->withHeader('Authorization', 'Bearer ' . $this->tokens[$userId])->json($method, $uri, $data);
    }

    private function uniqueTag(): string
    {
        return 'T' . substr(md5(uniqid((string)mt_rand(), true)), 0, 5);
    }

    private function uniqueName(): string
    {
        return 'Api Alliance ' . substr(md5(uniqid((string)mt_rand(), true)), 0, 8);
    }

    /**
     * Found an alliance as the test player through the API.
     */
    private function found(): int
    {
        $response = $this->api('POST', '/api/v1/alliance', ['tag' => $this->uniqueTag(), 'name' => $this->uniqueName()])
            ->assertStatus(201);

        return (int)$response->json('data.alliance.id');
    }

    public function testMineIsNullWithoutAlliance(): void
    {
        $this->api('GET', '/api/v1/alliance')->assertOk()->assertJsonPath('data.alliance', null);
    }

    public function testFoundAnAllianceAndSeeItAsMine(): void
    {
        $tag = $this->uniqueTag();
        $name = $this->uniqueName();

        $this->api('POST', '/api/v1/alliance', ['tag' => $tag, 'name' => $name])
            ->assertStatus(201)
            ->assertJsonPath('data.alliance.tag', $tag);

        $mine = $this->api('GET', '/api/v1/alliance')->assertOk();
        $mine->assertJsonPath('data.alliance.name', $name);
        $mine->assertJsonPath('data.alliance.is_founder', true);
        $mine->assertJsonPath('data.alliance.member_count', 1);
        $mine->assertJsonPath('data.alliance.members.0.user_id', $this->currentUserId);
        $this->assertSame([], $mine->json('data.alliance.applications'));
        $this->assertContains('edit_applications', $mine->json('data.alliance.my_permissions'));
    }

    public function testTagAndNameRulesAndUniqueness(): void
    {
        $this->api('POST', '/api/v1/alliance', ['tag' => 'ab', 'name' => 'Valid name'])
            ->assertStatus(422)->assertJsonPath('error.code', 'validation_failed');
        $this->api('POST', '/api/v1/alliance', ['tag' => 'TOOLONGTAG', 'name' => 'Valid name'])
            ->assertStatus(422)->assertJsonPath('error.code', 'validation_failed');

        $tag = $this->uniqueTag();
        $name = $this->uniqueName();
        $other = $this->createUser();
        resolve(AllianceService::class)->createAlliance($other->id, $tag, $name);

        $this->api('POST', '/api/v1/alliance', ['tag' => $tag, 'name' => $this->uniqueName()])
            ->assertStatus(422)->assertJsonPath('error.code', 'tag_taken');
        $this->api('POST', '/api/v1/alliance', ['tag' => $this->uniqueTag(), 'name' => $name])
            ->assertStatus(422)->assertJsonPath('error.code', 'name_taken');
    }

    public function testCannotFoundTwice(): void
    {
        $this->found();

        $this->api('POST', '/api/v1/alliance', ['tag' => $this->uniqueTag(), 'name' => $this->uniqueName()])
            ->assertStatus(422)->assertJsonPath('error.code', 'already_in_alliance');
    }

    public function testPublicInfoAndListing(): void
    {
        $id = $this->found();

        $this->api('GET', '/api/v1/alliances/' . $id)
            ->assertOk()
            ->assertJsonPath('data.alliance.id', $id)
            ->assertJsonPath('data.alliance.members.0.is_founder', true);
        $this->api('GET', '/api/v1/alliances/99999999')->assertStatus(404)->assertJsonPath('error.code', 'alliance_not_found');

        $ids = array_column($this->api('GET', '/api/v1/alliances?open_only=1&per_page=100')->assertOk()->json('data.alliances'), 'id');
        $this->assertContains($id, $ids);
    }

    public function testApplyAndTheFounderAccepts(): void
    {
        $id = $this->found();
        $applicant = $this->createUser();

        $this->api('POST', '/api/v1/alliances/' . $id . '/apply', ['text' => 'peaceful miner'], $applicant->id)
            ->assertStatus(201)
            ->assertJsonPath('data.application.status', 'pending');
        $this->api('POST', '/api/v1/alliances/' . $id . '/apply', [], $applicant->id)
            ->assertStatus(422)->assertJsonPath('error.code', 'application_pending');

        $applications = $this->api('GET', '/api/v1/alliance')->json('data.alliance.applications');
        $this->assertCount(1, $applications);
        $this->assertSame('peaceful miner', $applications[0]['text']);

        // The applicant is no member yet, so cannot accept.
        $this->api('POST', '/api/v1/alliance/applications/' . $applications[0]['id'] . '/accept', [], $applicant->id)
            ->assertStatus(404);

        $this->api('POST', '/api/v1/alliance/applications/' . $applications[0]['id'] . '/accept')
            ->assertOk()->assertJsonPath('data.application.status', 'accepted');
        $this->assertSame($id, User::findOrFail($applicant->id)->alliance_id);
        $this->api('POST', '/api/v1/alliance/applications/' . $applications[0]['id'] . '/accept')
            ->assertStatus(422)->assertJsonPath('error.code', 'application_not_pending');
    }

    public function testRejectAnApplication(): void
    {
        $id = $this->found();
        $applicant = $this->createUser();
        $application = $this->api('POST', '/api/v1/alliances/' . $id . '/apply', [], $applicant->id)->json('data.application');

        $this->api('POST', '/api/v1/alliance/applications/' . $application['id'] . '/reject')
            ->assertOk()->assertJsonPath('data.application.status', 'rejected');
        $this->assertNull(User::findOrFail($applicant->id)->alliance_id);
    }

    public function testMemberLeavesAndFounderCannot(): void
    {
        $id = $this->found();
        $member = $this->createUser();
        $application = $this->api('POST', '/api/v1/alliances/' . $id . '/apply', [], $member->id)->json('data.application');
        $this->api('POST', '/api/v1/alliance/applications/' . $application['id'] . '/accept')->assertOk();

        $this->api('POST', '/api/v1/alliance/leave')
            ->assertStatus(422)->assertJsonPath('error.code', 'founder_protected');
        $this->api('POST', '/api/v1/alliance/leave', [], $member->id)->assertOk()->assertJsonPath('data.left', true);
        $this->api('POST', '/api/v1/alliance/leave', [], $member->id)
            ->assertStatus(422)->assertJsonPath('error.code', 'not_in_alliance');

        // The leave cooldown applies to the leaver.
        $this->api('POST', '/api/v1/alliances/' . $id . '/apply', [], $member->id)
            ->assertStatus(422)->assertJsonPath('error.code', 'alliance_cooldown');
    }

    public function testKickNeedsPermission(): void
    {
        $id = $this->found();
        $first = $this->createUser();
        $second = $this->createUser();
        foreach ([$first, $second] as $user) {
            $application = $this->api('POST', '/api/v1/alliances/' . $id . '/apply', [], $user->id)->json('data.application');
            $this->api('POST', '/api/v1/alliance/applications/' . $application['id'] . '/accept')->assertOk();
        }

        // A member without a rank may not kick.
        $this->api('POST', '/api/v1/alliance/kick', ['user_id' => $second->id], $first->id)
            ->assertStatus(403)->assertJsonPath('error.code', 'no_permission');
        // The founder cannot be kicked, but a member can.
        $this->api('POST', '/api/v1/alliance/kick', ['user_id' => $this->currentUserId])
            ->assertStatus(422)->assertJsonPath('error.code', 'founder_protected');
        $this->api('POST', '/api/v1/alliance/kick', ['user_id' => $second->id])->assertOk();
        $this->assertNull(User::findOrFail($second->id)->alliance_id);
    }

    public function testBroadcastReachesTheOtherMembers(): void
    {
        $id = $this->found();
        $member = $this->createUser();
        $application = $this->api('POST', '/api/v1/alliances/' . $id . '/apply', [], $member->id)->json('data.application');
        $this->api('POST', '/api/v1/alliance/applications/' . $application['id'] . '/accept')->assertOk();

        $this->api('POST', '/api/v1/alliance/broadcast', ['message' => 'INTEL 1:2:3'])->assertOk();
        $this->assertSame(1, Message::where('user_id', $member->id)->where('key', 'alliance_broadcast')->count());

        // A rankless member has no circular message permission.
        $this->api('POST', '/api/v1/alliance/broadcast', ['message' => 'hi'], $member->id)
            ->assertStatus(403)->assertJsonPath('error.code', 'no_permission');
        $this->api('POST', '/api/v1/alliance/broadcast', [])->assertStatus(422);
    }

    public function testAllianceChatAndPrivateMessages(): void
    {
        $id = $this->found();
        $member = $this->createUser();
        $outsider = $this->createUser();
        $application = $this->api('POST', '/api/v1/alliances/' . $id . '/apply', [], $member->id)->json('data.application');
        $this->api('POST', '/api/v1/alliance/applications/' . $application['id'] . '/accept')->assertOk();

        $sent = $this->api('POST', '/api/v1/chat/send', ['alliance' => true, 'text' => 'WATCH 1:2:3'], $member->id)
            ->assertStatus(201)->json('data.message');
        $history = $this->api('GET', '/api/v1/chat?alliance=1')->assertOk();
        $this->assertSame('WATCH 1:2:3', $history->json('data.messages.0.text'));
        $this->assertSame([], $this->api('GET', '/api/v1/chat?alliance=1&since_id=' . $sent['id'])->json('data.messages'));

        $this->api('POST', '/api/v1/chat/send', ['alliance' => true, 'text' => 'hi'], $outsider->id)
            ->assertStatus(422)->assertJsonPath('error.code', 'not_in_alliance');

        $this->api('POST', '/api/v1/messages/send', ['user_id' => $outsider->id, 'text' => 'hello'])->assertStatus(201);
        $this->api('GET', '/api/v1/chat?with=' . $this->currentUserId, [], $outsider->id)
            ->assertOk()->assertJsonPath('data.messages.0.text', 'hello');
        $this->api('POST', '/api/v1/messages/send', ['user_id' => $this->currentUserId, 'text' => 'me'])
            ->assertStatus(422)->assertJsonPath('error.code', 'same_user');
        $this->api('POST', '/api/v1/messages/send', ['user_id' => 99999999, 'text' => 'x'])
            ->assertStatus(404)->assertJsonPath('error.code', 'player_not_found');
        $this->assertSame(1, Alliance::where('id', $id)->count());
    }
}
