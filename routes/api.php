<?php

use Illuminate\Support\Facades\Route;
use OGame\Http\Controllers\Api\V1\AuthController;
use OGame\Http\Controllers\Api\V1\BuildingController;
use OGame\Http\Controllers\Api\V1\FleetController;
use OGame\Http\Controllers\Api\V1\GalaxyController;
use OGame\Http\Controllers\Api\V1\HighscoreController;
use OGame\Http\Controllers\Api\V1\MessageController;
use OGame\Http\Controllers\Api\V1\PlanetController;
use OGame\Http\Controllers\Api\V1\ResearchController;
use OGame\Http\Controllers\Api\V1\StatusController;
use OGame\Http\Controllers\Api\V1\UnitController;
use OGame\Http\Controllers\Api\V1\UniverseController;

/*
|--------------------------------------------------------------------------
| JSON API routes (/api/v1)
|--------------------------------------------------------------------------
|
| Machine-readable API for bots and AI agents. The "/api/v1" prefix is configured in
| bootstrap/app.php. See docs/API.md for the reference.
|
*/

// Public.
Route::get('/universe', [UniverseController::class, 'show']);

Route::middleware('throttle:ogamex-api-auth')->group(function () {
    Route::post('/auth/register', [AuthController::class, 'register']);
    Route::post('/auth/login', [AuthController::class, 'login']);
});

// Authenticated: bearer token -> current player. "globalgame" then runs the same per-request
// updates as the web UI (resource production, finished queues, arrived fleets).
Route::middleware(['api.token', 'throttle:ogamex-api', 'globalgame'])->group(function () {
    Route::post('/auth/logout', [AuthController::class, 'logout']);

    Route::get('/me', [StatusController::class, 'me']);
    Route::get('/status', [StatusController::class, 'status']);

    Route::get('/planets', [PlanetController::class, 'index']);
    Route::get('/planets/{planet}', [PlanetController::class, 'show'])->whereNumber('planet');

    Route::get('/planets/{planet}/buildings', [BuildingController::class, 'index'])->whereNumber('planet');
    Route::post('/planets/{planet}/buildings', [BuildingController::class, 'build'])->whereNumber('planet');
    Route::get('/planets/{planet}/buildings/queue', [BuildingController::class, 'queue'])->whereNumber('planet');
    Route::delete('/planets/{planet}/buildings/queue/{queueId}', [BuildingController::class, 'cancel'])
        ->whereNumber(['planet', 'queueId']);

    Route::get('/planets/{planet}/research', [ResearchController::class, 'index'])->whereNumber('planet');
    Route::post('/planets/{planet}/research', [ResearchController::class, 'start'])->whereNumber('planet');
    Route::get('/research/queue', [ResearchController::class, 'queue']);
    Route::delete('/research/queue/{queueId}', [ResearchController::class, 'cancel'])->whereNumber('queueId');

    Route::get('/planets/{planet}/units', [UnitController::class, 'index'])->whereNumber('planet');
    Route::post('/planets/{planet}/units', [UnitController::class, 'build'])->whereNumber('planet');
    Route::get('/planets/{planet}/units/queue', [UnitController::class, 'queue'])->whereNumber('planet');

    Route::get('/galaxy/{galaxy}/{system}', [GalaxyController::class, 'show'])->whereNumber(['galaxy', 'system']);

    Route::get('/ships', [FleetController::class, 'ships']);
    Route::get('/fleet', [FleetController::class, 'index']);
    Route::post('/planets/{planet}/fleet/check', [FleetController::class, 'check'])->whereNumber('planet');
    Route::post('/planets/{planet}/fleet', [FleetController::class, 'send'])->whereNumber('planet');
    Route::post('/fleet/{mission}/recall', [FleetController::class, 'recall'])->whereNumber('mission');

    Route::get('/messages', [MessageController::class, 'index']);
    Route::get('/messages/{message}', [MessageController::class, 'show'])->whereNumber('message');
    Route::post('/messages/{message}/read', [MessageController::class, 'markRead'])->whereNumber('message');

    Route::get('/highscore', [HighscoreController::class, 'index']);
});
