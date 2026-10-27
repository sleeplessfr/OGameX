<?php

/*
|--------------------------------------------------------------------------
| JSON API (/api/v1)
|--------------------------------------------------------------------------
|
| Settings for the machine-readable API used by bots and AI agents.
| Every value can be overridden in the .env file.
|
*/

return [
    // Allow new accounts to be created through POST /api/v1/auth/register.
    'registration_enabled' => (bool) env('OGAMEX_API_REGISTRATION', true),

    // Lifetime of newly issued tokens in days. 0 means tokens never expire.
    'token_ttl_days' => (int) env('OGAMEX_API_TOKEN_TTL_DAYS', 0),

    // Maximum authenticated requests per minute, per account.
    'rate_limit_per_minute' => (int) env('OGAMEX_API_RATE_LIMIT', 300),

    // Maximum register/login requests per minute, per IP address.
    'auth_rate_limit_per_minute' => (int) env('OGAMEX_API_AUTH_RATE_LIMIT', 30),

    // Performance profiler for /api/v1 (see docs/PROFILING.md). When true, one JSON line per request is
    // appended to storage/logs/api-profile.jsonl. Leave false in normal operation.
    'profile' => (bool) env('API_PROFILE', false),

    // Optional path of the profile log (default storage/logs/api-profile.jsonl). The tests set it to a temporary file.
    'profile_log' => env('API_PROFILE_LOG'),
];
