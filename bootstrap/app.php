<?php

use Illuminate\Foundation\Application;
use Illuminate\Foundation\Configuration\Exceptions;
use Illuminate\Foundation\Configuration\Middleware;
use OGame\Exceptions\ApiException;
use OGame\Http\Middleware\Admin;
use OGame\Http\Middleware\ApiProfiler;
use OGame\Http\Middleware\AuthenticateApiToken;
use OGame\Http\Middleware\CheckBanned;
use OGame\Http\Middleware\CheckFirstLogin;
use OGame\Http\Middleware\ForceJsonResponse;
use OGame\Http\Middleware\GlobalGame;
use OGame\Http\Middleware\Locale;
use OGame\Http\Middleware\ServerTiming;

return Application::configure(basePath: dirname(__DIR__))
    ->withRouting(
        web: __DIR__.'/../routes/web.php',
        api: __DIR__.'/../routes/api.php',
        apiPrefix: 'api/v1',
        commands: __DIR__.'/../routes/console.php',
        health: '/up',
    )
    ->withBroadcasting(
        __DIR__.'/../routes/channels.php',
        ['middleware' => ['web', 'auth']],
    )
    ->withMiddleware(function (Middleware $middleware) {
        $middleware->prepend(ServerTiming::class);
        // Locale must be APPENDED (not prepended) to the web group so that it executes
        // after StartSession. Prepending would place it before StartSession, making
        // $request->hasSession() return false and breaking session-based locale reading.
        // Appending still guarantees the locale is set before any route-specific middleware
        // (auth, globalgame, firstlogin) runs.
        $middleware->web(append: [Locale::class]);
        // JSON API (/api/v1): always answer with JSON, never with HTML or redirects.
        $middleware->api(prepend: [ForceJsonResponse::class]);
        // API profiler (docs/PROFILING.md). Registered always because config is not loaded yet at this
        // point; it checks config('ogamex_api.profile') itself and does nothing when that is false.
        $middleware->api(prepend: [ApiProfiler::class]);
        $middleware->alias([
            'api.token' => AuthenticateApiToken::class,
            'globalgame' => GlobalGame::class,
            'locale' => Locale::class,
            'admin' => Admin::class,
            'firstlogin' => CheckFirstLogin::class,
            'banned' => CheckBanned::class,
        ]);
    })
    ->withExceptions(function (Exceptions $exceptions) {
        // API errors (wrong input, not enough resources, ...) are expected, not bugs: do not log them
        // and do not send them to Discord.
        $exceptions->dontReport(ApiException::class);
    })
    ->create();
