# Profiling the API (why is every call slow?)

The profiler writes one line per API call into `storage/logs/api-profile.jsonl`:
how long the call took, how many database queries it ran, which queries were slowest
or repeated most, and how long each step of the request took.
It changes nothing in the game. When it is off it does nothing.

## Switch it on

1. In the `.env` file add the line `API_PROFILE=true`
2. In a terminal in the project folder:
   - `docker compose restart`
   - `docker compose run --rm ogamex-app php artisan optimize:clear`
3. Run your benchmark / let the bots play for a few minutes.

## Read the result

`docker compose exec ogamex-app php artisan api:profile-report`

Useful options: `--lines=2000` (only the last 2000 calls) and `--sql-sample` (show the slowest query of each route).

The report shows:
- one row per route: how many calls, average and p95 time, queries per call, time in the database, response size;
- the phase table: how many ms were spent in each step (`boot` = Laravel start-up, `auth+throttle` = token check,
  `load player`, `player update`, `planet update`, `fleet missions`, `moves` = the GlobalGame middleware,
  `controller` = the actual endpoint);
- the 10 most repeated queries;
- the top 3 suspects in plain English.

Start over with `docker compose exec ogamex-app php artisan api:profile-clear`.

## Switch it off

Remove the line `API_PROFILE=true` (or set it to `false`), run `docker compose restart` and
`docker compose run --rm ogamex-app php artisan optimize:clear` again. Delete the log with `api:profile-clear`.

## Speed settings

What the server owner applied after the first measurements (per request CPU is the limit, not the database):

- **OPcache sizes**: `php/zz-opcache.ini` (`opcache.max_accelerated_files=30000`, `opcache.memory_consumption=256`,
  `opcache.interned_strings_buffer=16`), mounted into the app container by this line of `docker-compose.yml`
  (service `ogamex-app`): `- ./php/zz-opcache.ini:/usr/local/etc/php/conf.d/zz-opcache.ini`.
  OPcache itself must be on in the image (the compose file builds the dev image with `OPCACHE_ENABLE: "0"`; check
  `docker compose exec ogamex-app php -i | grep opcache.enable` if the ini seems to have no effect).
- **Config and route cache**: `docker compose exec ogamex-app php artisan config:cache` and `php artisan route:cache`.
  Run `php artisan optimize:clear` before changing `.env` or routes, then cache again.
- **Optional trial, not applied by default**: `CACHE_STORE=file` in `.env`. The default cache store is the database, and the
  rate limiter and other cache reads cost a `select * from cache` query each (about 4 per request). Switching to `file`
  removes those queries; run `php artisan config:cache` again after the change and compare with the profiler.
  Nothing in the code depends on it, so it is easy to revert.

## Code-reading suspects (not measured yet)

These come from reading the code only. The numbers will tell which one is real.

1. **Game objects are rebuilt from scratch on every lookup (best suspect for the PHP CPU).**
   `ObjectService::getObjectByMachineName` / `getObjectById` / `getObjects` / `getGameObjectsWithProduction`
   (`app/Services/ObjectService.php` lines 40, 169, 190, 286, 305, 321) call `BuildingObjects::get()`, `StationObjects::get()`,
   `ResearchObjects::get()`, ... every time. Each of those creates about 60 objects with about 200 translated texts
   (`__()` calls, `app/GameObjects/*Objects.php`) and many closures, and then the caller throws all of it away after finding one object.
   The same pattern is in `getResearchObjectByMachineName` (line 211), `getUnitObjectByMachineName` (line 267).
   These lookups are used in tight loops: `PlanetService::getObjectLevel` (line 1132), `getBuildingPercent` (2505),
   `getObjectAmount` (2721), `PlayerService::getResearchLevel` (line 437, called by every production calculation in
   `GameObjectProduction::calculatePlasmaTech`, `app/GameObjects/Models/Fields/GameObjectProduction.php` line 187).
   A fix later: build the object lists once per process (static cache) and index them by machine name and id.
2. **Planet update does a lot of work and runs for every planet.**
   `PlanetService::update` (line 1318) runs on the current planet in `GlobalGame` (line 62) and again in the status
   endpoint for every planet (`StatusController::status` line 47) and in `ApiController::planetFor` (line 72).
   Each update = lock query + reload of the whole planet row + `updateResourceProductionStats` (line 2123, which loops all
   production objects through suspect 1 and again loops them a second time for the fusion plant, lines 2274 and 2305)
   + `updateResourceStorageStats` (line 2632, uses `eval()` per storage building, line 2655) + a save. The same planet is updated
   twice per request (middleware, then controller).
3. **Several queries and full planet rows per request.** `GlobalGame` (`app/Http/Middleware/GlobalGame.php`) does for each call:
   load the user with highscore and tech (`PlayerService::load`, line 96), load all planets (`PlanetListService` constructor, line 40,
   wide rows), `PlayerService::update` (line 635: transaction, lock, user save), `updateFleetMissions` (line 709: transaction,
   locks ALL planets of the player with `lockForUpdate`, queries missions), `processDueMoves` (`PlanetMoveService` line 359).
   These are the likely source of the 1 MB per request sent by MariaDB (wide `planets` rows read several times, `lockForUpdate` + reload).
   After a fleet mission has arrived, `updateFleetMissions` reloads the whole player again (line 746).
4. **Writes on every request.** `AuthenticateApiToken` calls `touchLastUsed` (a write per call), `PlayerService::update` saves the user
   (time, last_ip), planet update saves the planet. Rate limiting (`throttle:ogamex-api`) uses the cache store, which defaults to `database`
   (`config/cache.php` line 17): extra reads and writes on every call.
5. **`/status` scales with the number of planets.** For each planet it runs update, 2 queue queries
   (`BuildingQueueService::retrieveQueue` line 262, `UnitQueueService`), plus `getFleetSlotsInUse` and `getExpeditionSlotsInUse`
   (`PlayerService` lines 540 and 587) each resolve `FleetMissionService` and run the same active-missions query separately.
6. **Settings** are read with `Setting::all()` once per request (`SettingsService` line 39): small, but check the repeated-query list.

Not checked: `hasCommander()` / `hasAdmiral()` in `PlayerService` (could hit the database inside the production loop), the web-only
middleware, and whether PHP-FPM / opcache settings (for example `opcache.validate_timestamps`, JIT) matter.

## Fast cache on every start (`OGAMEX_FAST_CACHE`)

`composer install`, which the game runs on every container start, deletes the cached configuration. So a hand-made
`php artisan config:cache` is lost at the next restart and the game becomes slow again without any message.
Add the line `OGAMEX_FAST_CACHE=true` to `.env`: the start-up script then rebuilds the config and route caches itself,
after `composer install`. When you change `.env` or the routes, just restart the game.
To go back to the normal behaviour, remove the line and run `php artisan optimize:clear`.
