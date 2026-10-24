<?php

namespace OGame\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Support\Carbon;
use Illuminate\Support\Str;

/**
 * Bearer token that authenticates a player against the JSON API (/api/v1).
 *
 * Only the SHA-256 hash of a token is stored. The plain text token is returned exactly once,
 * when it is issued.
 *
 * @property int $id
 * @property int $user_id
 * @property string $name
 * @property string $token_hash
 * @property Carbon|null $last_used_at
 * @property Carbon|null $expires_at
 * @property Carbon|null $created_at
 * @property Carbon|null $updated_at
 * @property-read User $user
 */
class ApiToken extends Model
{
    /**
     * Every plain token starts with this prefix so it is easy to recognise in logs and secret scanners.
     */
    public const string PREFIX = 'ogx_';

    /**
     * @var string
     */
    protected $table = 'api_tokens';

    /**
     * @var array<int, string>
     */
    protected $fillable = ['user_id', 'name', 'token_hash', 'last_used_at', 'expires_at'];

    /**
     * @return array<string, string>
     */
    protected function casts(): array
    {
        return [
            'last_used_at' => 'datetime',
            'expires_at' => 'datetime',
        ];
    }

    /**
     * Create a token for a user.
     *
     * @param User $user
     * @param string $name Label to tell tokens apart (e.g. the name of the agent).
     * @param int $ttlDays Lifetime in days, 0 means the token never expires.
     * @return array{0: ApiToken, 1: string} The stored token and its plain text value.
     */
    public static function issue(User $user, string $name, int $ttlDays): array
    {
        $plain = self::PREFIX . Str::random(48);

        $token = self::create([
            'user_id' => $user->id,
            'name' => $name,
            'token_hash' => self::hashToken($plain),
            'expires_at' => $ttlDays > 0 ? now()->addDays($ttlDays) : null,
        ]);

        return [$token, $plain];
    }

    /**
     * Find a token that exists and has not expired.
     *
     * @param string $plain
     * @return ApiToken|null
     */
    public static function findByPlainText(string $plain): ApiToken|null
    {
        if (!str_starts_with($plain, self::PREFIX)) {
            return null;
        }

        /** @var ApiToken|null $token */
        $token = self::where('token_hash', self::hashToken($plain))->first();
        if ($token === null) {
            return null;
        }

        if ($token->expires_at !== null && $token->expires_at->isPast()) {
            return null;
        }

        return $token;
    }

    /**
     * Record that the token was used. Writes at most once per minute so busy agents
     * do not cause a database write on every request.
     */
    public function touchLastUsed(): void
    {
        if ($this->last_used_at === null || $this->last_used_at->lt(now()->subMinute())) {
            $this->last_used_at = now();
            $this->save();
        }
    }

    /**
     * Hash a plain token for storage and lookup.
     *
     * @param string $plain
     * @return string
     */
    public static function hashToken(string $plain): string
    {
        return hash('sha256', $plain);
    }

    /**
     * @return BelongsTo<User, $this>
     */
    public function user(): BelongsTo
    {
        return $this->belongsTo(User::class);
    }
}
