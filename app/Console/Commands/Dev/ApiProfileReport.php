<?php

namespace OGame\Console\Commands\Dev;

use Illuminate\Console\Attributes\Description;
use Illuminate\Console\Attributes\Signature;
use Illuminate\Console\Command;
use OGame\Http\Middleware\ApiProfiler;

#[Description('Summarise the API profile log: per route timings, repeated queries and top suspects.')]
#[Signature('api:profile-report
                            {--lines=5000 : Only read the last N requests of the log}
                            {--sql-sample : Also show the slowest query of each route}')]
class ApiProfileReport extends Command
{
    public function handle(): int
    {
        $path = ApiProfiler::logPath();
        if (!is_file($path)) {
            $this->warn('No profile log yet. Set API_PROFILE=true in .env, restart, and make some API calls (docs/PROFILING.md).');
            return self::SUCCESS;
        }

        $rows = $this->readLastRows($path, max(1, (int)$this->option('lines')));
        if (count($rows) === 0) {
            $this->warn('The profile log is empty.');
            return self::SUCCESS;
        }

        // Group by "METHOD /route/{pattern}".
        $routes = [];
        $queries = [];
        foreach ($rows as $row) {
            $key = ($row['method'] ?? '?') . ' ' . ($row['route'] ?? '?');
            if (!isset($routes[$key])) {
                $routes[$key] = ['ms' => [], 'queries' => 0, 'db_ms' => 0.0, 'bytes' => 0, 'phases' => [], 'slow' => null];
            }
            $r = &$routes[$key];
            $r['ms'][] = (float)($row['total_ms'] ?? 0);
            $r['queries'] += (int)($row['db_queries'] ?? 0);
            $r['db_ms'] += (float)($row['db_ms'] ?? 0);
            $r['bytes'] += (int)($row['response_bytes'] ?? 0);
            foreach (($row['phases_ms'] ?? []) as $name => $ms) {
                $r['phases'][$name] = ($r['phases'][$name] ?? 0.0) + (float)$ms;
            }
            foreach (($row['slowest_queries'] ?? []) as $q) {
                if ($r['slow'] === null || (float)$q['ms'] > (float)$r['slow']['ms']) {
                    $r['slow'] = $q;
                }
            }
            unset($r);

            foreach (($row['repeated_queries'] ?? []) as $q) {
                $sql = (string)($q['sql'] ?? '');
                if (!isset($queries[$sql])) {
                    $queries[$sql] = ['count' => 0, 'ms' => 0.0, 'routes' => []];
                }
                $queries[$sql]['count'] += (int)$q['count'];
                $queries[$sql]['ms'] += (float)$q['ms'];
                $queries[$sql]['routes'][$key] = ($queries[$sql]['routes'][$key] ?? 0) + (int)$q['count'];
            }
        }

        $this->info('Requests analysed: ' . count($rows));
        $this->newLine();

        // Per-route table.
        $table = [];
        $stats = [];
        foreach ($routes as $key => $r) {
            $n = count($r['ms']);
            sort($r['ms']);
            $p95 = $r['ms'][(int)min($n - 1, floor($n * 0.95))];
            $avg = array_sum($r['ms']) / $n;
            $stats[$key] = [
                'n' => $n,
                'avg' => $avg,
                'queries' => $r['queries'] / $n,
                'db_ms' => $r['db_ms'] / $n,
            ];
            $table[] = [$key, $n, round($avg, 1), round($p95, 1), round($r['queries'] / $n, 1), round($r['db_ms'] / $n, 1), round($r['bytes'] / $n / 1024, 1)];
        }
        usort($table, fn (array $a, array $b) => $b[1] * $b[2] <=> $a[1] * $a[2]);
        $this->table(['Route', 'Count', 'Avg ms', 'p95 ms', 'Avg queries', 'Avg DB ms', 'Avg KB'], $table);

        // Where the time goes inside a request.
        $this->newLine();
        $this->line('Average ms per phase (bootstrap = before the profiler; gg_* = GlobalGame middleware steps; controller_end = controller):');
        $phaseTable = [];
        foreach ($routes as $key => $r) {
            $n = count($r['ms']);
            $line = [$key];
            foreach (['bootstrap', 'gg_start', 'gg_player_loaded', 'gg_player_updated', 'gg_planet_updated', 'gg_fleet_updated', 'gg_end', 'controller_end'] as $phase) {
                $line[] = round(($r['phases'][$phase] ?? 0) / $n, 1);
            }
            $phaseTable[] = $line;
        }
        $this->table(['Route', 'boot', 'auth+throttle', 'load player', 'player update', 'planet update', 'fleet missions', 'moves', 'controller'], $phaseTable);

        // Most repeated queries.
        $this->newLine();
        $this->info('Most repeated queries (counted from the top 5 of each request):');
        uasort($queries, fn (array $a, array $b) => $b['count'] <=> $a['count']);
        $qTable = [];
        foreach (array_slice($queries, 0, 10, true) as $sql => $q) {
            arsort($q['routes']);
            $routeList = implode(', ', array_slice(array_keys($q['routes']), 0, 3));
            $qTable[] = [$q['count'], round($q['ms'], 1), mb_substr((string)$sql, 0, 110), $routeList];
        }
        $this->table(['Times', 'Total ms', 'Query', 'Routes'], $qTable);

        // Suspects.
        $this->newLine();
        $this->info('Top suspects:');
        $suspects = $this->suspects($stats, $queries, count($rows));
        if (count($suspects) === 0) {
            $this->line('Nothing stands out yet. Collect more requests.');
        }
        foreach ($suspects as $i => $text) {
            $this->line(($i + 1) . '. ' . $text);
        }

        if ($this->option('sql-sample')) {
            $this->newLine();
            $this->info('Slowest single query per route:');
            foreach ($routes as $key => $r) {
                if ($r['slow'] !== null) {
                    $this->line($key . '  (' . $r['slow']['ms'] . ' ms)');
                    $this->line('    ' . $r['slow']['sql']);
                    $this->line('    bindings: ' . $r['slow']['bindings']);
                }
            }
        }

        return self::SUCCESS;
    }

    /**
     * @param array<string, array{n: int, avg: float, queries: float, db_ms: float}> $stats
     * @param array<string, array{count: int, ms: float, routes: array<string, int>}> $queries
     * @return array<int, string>
     */
    private function suspects(array $stats, array $queries, int $totalRequests): array
    {
        $candidates = [];

        // Query repeated many times per request on one route (N+1).
        foreach ($queries as $sql => $q) {
            foreach ($q['routes'] as $route => $count) {
                $n = $stats[$route]['n'] ?? 1;
                $perRequest = $count / $n;
                if ($perRequest >= 5) {
                    $candidates[] = [
                        $perRequest * ($q['ms'] / max(1, $q['count'])) + $perRequest,
                        sprintf('%s runs %.0f queries per request that look the same (%s): probably N+1, one query per row instead of one for all rows.', $route, $perRequest, mb_substr((string)$sql, 0, 90)),
                    ];
                }
            }
        }

        foreach ($stats as $route => $s) {
            // Many queries in total.
            if ($s['queries'] >= 30) {
                $candidates[] = [
                    $s['queries'],
                    sprintf('%s runs %.0f database queries per request (avg %.0f ms of %.0f ms in the database).', $route, $s['queries'], $s['db_ms'], $s['avg']),
                ];
            }
            // Slow but little database time: the CPU is spent in PHP.
            if ($s['avg'] >= 100 && $s['db_ms'] < $s['avg'] * 0.3) {
                $candidates[] = [
                    $s['avg'] - $s['db_ms'],
                    sprintf('%s takes %.0f ms but only %.0f ms is database: the time is PHP work (look at the phase table).', $route, $s['avg'], $s['db_ms']),
                ];
            }
        }

        usort($candidates, fn (array $a, array $b) => $b[0] <=> $a[0]);

        return array_map(fn (array $c) => $c[1], array_slice($candidates, 0, 3));
    }

    /**
     * Read the last $max valid JSON lines.
     *
     * @return array<int, array<string, mixed>>
     */
    private function readLastRows(string $path, int $max): array
    {
        $handle = fopen($path, 'rb');
        if ($handle === false) {
            return [];
        }

        $buffer = [];
        while (($line = fgets($handle)) !== false) {
            $buffer[] = $line;
            if (count($buffer) > $max) {
                array_shift($buffer);
            }
        }
        fclose($handle);

        $rows = [];
        foreach ($buffer as $line) {
            $decoded = json_decode($line, true);
            if (is_array($decoded)) {
                $rows[] = $decoded;
            }
        }

        return $rows;
    }
}
