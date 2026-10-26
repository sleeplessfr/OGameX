<?php

namespace OGame\Providers;

use Illuminate\Cache\RateLimiting\Limit;
use Illuminate\Contracts\Debug\ExceptionHandler;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\RateLimiter;
use Illuminate\Support\Facades\URL;
use Illuminate\Support\Facades\Vite;
use Illuminate\Support\ServiceProvider;
use OGame\Exceptions\Handler;
use OGame\Factories\PlanetServiceFactory;
use OGame\Factories\PlayerServiceFactory;
use OGame\Models\User;
use OGame\Observers\UserObserver;
use OGame\Services\SettingsService;

class AppServiceProvider extends ServiceProvider
{
    /**
     * Bootstrap any application services.
     *
     * @return void
     */
    final public function boot(): void
    {
        if ($this->app->environment('production')) {
            URL::forceScheme('https');
        }

        // Legacy JS bundles are plain concatenated scripts that rely on jQuery globals
        // and must not be treated as ES modules.
        Vite::useScriptTagAttributes(['type' => false]);

        // Register composer file for the main ingame layout.
        view()->composer('ingame.layouts.main', 'OGame\Http\ViewComposers\IngameMainComposer');

        // Register model observers
        User::observe(UserObserver::class);

        // Rate limits of the JSON API (/api/v1), configurable in config/ogamex_api.php.
        // Authenticated calls are limited per account, register/login per IP address.
        // The throttle middleware runs before authentication, so $request->user() is normally still null
        // here. Without a fallback every agent on one machine would share a single per-IP bucket, so the
        // bucket is keyed by a hash of the bearer token (one token = one account) when the request has one.
        RateLimiter::for('ogamex-api', function (Request $request) {
            $user = $request->user();
            $token = $request->bearerToken();

            if ($user !== null) {
                $key = 'user:' . $user->id;
            } elseif (!empty($token)) {
                $key = 'tok:' . sha1($token);
            } else {
                $key = 'ip:' . $request->ip();
            }

            return Limit::perMinute((int)config('ogamex_api.rate_limit_per_minute'))->by($key);
        });
        RateLimiter::for('ogamex-api-auth', function (Request $request) {
            return Limit::perMinute((int)config('ogamex_api.auth_rate_limit_per_minute'))
                ->by('ip:' . $request->ip());
        });
    }

    /**
     * Register any application services.
     *
     * @return void
     */
    final public function register(): void
    {
        $this->app->singleton(function ($app): SettingsService {
            return new SettingsService();
        });

        $this->app->singleton(function ($app): PlayerServiceFactory {
            return new PlayerServiceFactory();
        });

        $this->app->singleton(function ($app): PlanetServiceFactory {
            return new PlanetServiceFactory(
                $app->make(SettingsService::class),
                $app->make(PlayerServiceFactory::class)
            );
        });

        $this->app->singleton(ExceptionHandler::class, Handler::class);
    }
}
