<?php

namespace Tests\Feature\Api;

use OGame\Http\Middleware\ApiProfiler;
use OGame\Models\ApiToken;
use OGame\Models\User;
use Tests\IsolatedAccountTestCase;

/**
 * Tests of the API profiler (storage/logs/api-profile.jsonl).
 */
class ApiProfilerTest extends IsolatedAccountTestCase
{
    protected function setUp(): void
    {
        parent::setUp();
        $this->deleteLog();
    }

    protected function tearDown(): void
    {
        $this->deleteLog();
        parent::tearDown();
    }

    private function deleteLog(): void
    {
        if (is_file(ApiProfiler::logPath())) {
            unlink(ApiProfiler::logPath());
        }
    }

    private function callMe(): void
    {
        $user = User::findOrFail($this->currentUserId);
        [, $token] = ApiToken::issue($user, 'test', 0);

        $this->withHeader('Authorization', 'Bearer ' . $token)->json('GET', '/api/v1/me')->assertOk();
    }

    public function testNothingIsWrittenWhenDisabled(): void
    {
        config(['ogamex_api.profile' => false]);

        $this->callMe();

        $this->assertFileDoesNotExist(ApiProfiler::logPath());
    }

    public function testOneLineIsWrittenWhenEnabled(): void
    {
        config(['ogamex_api.profile' => true]);

        $this->callMe();

        $this->assertFileExists(ApiProfiler::logPath());
        $lines = file(ApiProfiler::logPath(), FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
        $this->assertCount(1, $lines);

        $row = json_decode($lines[0], true);
        foreach (['time', 'method', 'route', 'status', 'total_ms', 'phases_ms', 'db_queries', 'db_ms', 'memory_peak_mb', 'response_bytes', 'slowest_queries', 'repeated_queries'] as $key) {
            $this->assertArrayHasKey($key, $row);
        }
        $this->assertSame('GET', $row['method']);
        $this->assertSame('/api/v1/me', $row['route']);
        $this->assertSame(200, $row['status']);
        $this->assertGreaterThan(0, $row['db_queries']);
    }
}
