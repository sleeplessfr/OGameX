<?php

namespace OGame\Console\Commands\Dev;

use Illuminate\Console\Attributes\Description;
use Illuminate\Console\Attributes\Signature;
use Illuminate\Console\Command;
use OGame\Http\Middleware\ApiProfiler;

#[Description('Delete the API profile log (storage/logs/api-profile.jsonl).')]
#[Signature('api:profile-clear')]
class ApiProfileClear extends Command
{
    public function handle(): int
    {
        $path = ApiProfiler::logPath();
        if (is_file($path)) {
            unlink($path);
            $this->info('Profile log deleted.');
        } else {
            $this->info('No profile log found.');
        }

        return self::SUCCESS;
    }
}
