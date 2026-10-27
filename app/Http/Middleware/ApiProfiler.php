<?php

namespace OGame\Http\Middleware;

use Closure;
use Illuminate\Database\Events\QueryExecuted;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Symfony\Component\HttpFoundation\Response;
use Throwable;

/**
 * Measures where the time of a /api/v1 request goes. See docs/PROFILING.md.
 *
 * Does nothing at all unless config('ogamex_api.profile') is true (env API_PROFILE=true).
 * When on, it appends ONE JSON line per request to storage/logs/api-profile.jsonl.
 * It never changes the response and swallows its own errors.
 */
class ApiProfiler
{
    /** Whether the current request is being profiled. */
    private static bool $active = false;

    /** @var array<int, array{string, float}> Named time marks: [name, microtime]. */
    private static array $marks = [];

    /** @var array<string, array{count: int, ms: float}> Normalized query => stats. */
    private static array $groups = [];

    /** @var array<int, array{ms: float, sql: string, bindings: string}> */
    private static array $slow = [];

    private static int $queryCount = 0;
    private static float $queryMs = 0.0;

    /**
     * Full path of the profile log.
     */
    public static function logPath(): string
    {
        // 'profile_log' lets the tests write to a file of their own instead of the live log the running game fills.
        $custom = config('ogamex_api.profile_log');
        if (is_string($custom) && $custom !== '') {
            return $custom;
        }

        return storage_path('logs/api-profile.jsonl');
    }

    /**
     * Record a named time mark (used by GlobalGame). No-op when profiling is off.
     */
    public static function mark(string $name): void
    {
        if (!self::$active) {
            return;
        }
        self::$marks[] = [$name, microtime(true)];
    }

    /**
     * Turn a SQL string into a comparable shape: literals and IN lists become "?".
     */
    public static function normalizeSql(string $sql): string
    {
        $sql = preg_replace("/'(?:[^'\\\\]|\\\\.)*'/", '?', $sql) ?? $sql;
        $sql = preg_replace('/\b\d+(?:\.\d+)?\b/', '?', $sql) ?? $sql;
        $sql = preg_replace('/\(\s*\?(?:\s*,\s*\?)+\s*\)/', '(?)', $sql) ?? $sql;
        $sql = preg_replace('/\s+/', ' ', $sql) ?? $sql;

        return mb_substr(trim($sql), 0, 200);
    }

    /**
     * @param Closure(Request): Response $next
     */
    public function handle(Request $request, Closure $next): Response
    {
        if (!$this->enabled()) {
            return $next($request);
        }

        $start = microtime(true);
        try {
            self::$active = true;
            self::$marks = [['handle_start', $start]];
            self::$groups = [];
            self::$slow = [];
            self::$queryCount = 0;
            self::$queryMs = 0.0;
            DB::listen(function (QueryExecuted $query) {
                self::onQuery($query);
            });
        } catch (Throwable) {
            self::$active = false;
            return $next($request);
        }

        $response = $next($request);

        try {
            $end = microtime(true);
            self::$marks[] = ['controller_end', $end];
            self::$active = false;
            $this->write($request, $response, $start, $end);
        } catch (Throwable) {
            // Never break a request because of the profiler.
            self::$active = false;
        }

        return $response;
    }

    private function enabled(): bool
    {
        try {
            return (bool)config('ogamex_api.profile', false);
        } catch (Throwable) {
            return false;
        }
    }

    private static function onQuery(QueryExecuted $query): void
    {
        if (!self::$active) {
            return;
        }
        try {
            $ms = (float)$query->time;
            self::$queryCount++;
            self::$queryMs += $ms;

            $key = self::normalizeSql($query->sql);
            if (!isset(self::$groups[$key])) {
                self::$groups[$key] = ['count' => 0, 'ms' => 0.0];
            }
            self::$groups[$key]['count']++;
            self::$groups[$key]['ms'] += $ms;

            // Keep only the 5 slowest queries.
            if (count(self::$slow) < 5 || $ms > self::$slow[count(self::$slow) - 1]['ms']) {
                $bindings = json_encode($query->bindings);
                self::$slow[] = [
                    'ms' => round($ms, 2),
                    'sql' => mb_substr($query->sql, 0, 600),
                    'bindings' => mb_substr($bindings === false ? '' : $bindings, 0, 200),
                ];
                usort(self::$slow, fn (array $a, array $b) => $b['ms'] <=> $a['ms']);
                self::$slow = array_slice(self::$slow, 0, 5);
            }
        } catch (Throwable) {
            // Ignore.
        }
    }

    private function write(Request $request, Response $response, float $start, float $end): void
    {
        $laravelStart = defined('LARAVEL_START') ? (float)LARAVEL_START : $start;

        // Time per phase: difference between consecutive marks, named after the later mark.
        $phases = ['bootstrap' => round(($start - $laravelStart) * 1000, 1)];
        for ($i = 1; $i < count(self::$marks); $i++) {
            $phases[self::$marks[$i][0]] = round((self::$marks[$i][1] - self::$marks[$i - 1][1]) * 1000, 1);
        }

        // Most repeated normalized queries.
        $groups = self::$groups;
        uasort($groups, fn (array $a, array $b) => $b['count'] <=> $a['count']);
        $repeated = [];
        foreach (array_slice($groups, 0, 5, true) as $sql => $stats) {
            $repeated[] = ['sql' => (string)$sql, 'count' => $stats['count'], 'ms' => round($stats['ms'], 2)];
        }

        $route = $request->route();
        $content = $response->getContent();

        $line = [
            'time' => date('c'),
            'method' => $request->getMethod(),
            'route' => $route !== null ? '/' . ltrim($route->uri(), '/') : '(no route)',
            'status' => $response->getStatusCode(),
            'total_ms' => round(($end - $laravelStart) * 1000, 1),
            'phases_ms' => $phases,
            'db_queries' => self::$queryCount,
            'db_ms' => round(self::$queryMs, 1),
            'memory_peak_mb' => round(memory_get_peak_usage(true) / 1048576, 1),
            'response_bytes' => $content === false ? 0 : strlen($content),
            'slowest_queries' => self::$slow,
            'repeated_queries' => $repeated,
        ];

        $json = json_encode($line, JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE);
        if ($json !== false) {
            file_put_contents(self::logPath(), $json . "\n", FILE_APPEND | LOCK_EX);
        }
    }
}
