<?php

namespace OGame\Http\Middleware;

use Closure;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
use OGame\Exceptions\ApiException;
use OGame\Models\ApiToken;
use Symfony\Component\HttpFoundation\Response;

/**
 * Authenticates an API request with "Authorization: Bearer ogx_...".
 *
 * The owner of the token becomes the current user, which is all the existing game middleware
 * (GlobalGame) and services need to work exactly as they do for a logged in web session.
 */
class AuthenticateApiToken
{
    /**
     * Handle an incoming request.
     *
     * @param Request $request
     * @param Closure $next
     * @return Response
     * @throws ApiException
     */
    public function handle(Request $request, Closure $next): Response
    {
        $plain = $request->bearerToken();
        if ($plain === null || $plain === '') {
            throw new ApiException('unauthenticated', 'Missing bearer token. Send the header "Authorization: Bearer <token>".', 401);
        }

        $token = ApiToken::findByPlainText($plain);
        if ($token === null) {
            throw new ApiException('invalid_token', 'The token is invalid or has expired.', 401);
        }

        $user = $token->user;
        if ($user->isBanned()) {
            throw new ApiException('account_banned', 'This account is banned.', 403);
        }

        $token->touchLastUsed();

        Auth::setUser($user);
        $request->attributes->set('api_token', $token);

        return $next($request);
    }
}
