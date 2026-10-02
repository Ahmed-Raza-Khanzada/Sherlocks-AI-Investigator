<?php

namespace App\Support;

use RuntimeException;

/**
 * Short-lived token that lets a signed-in portal user's browser call the Sherlocks API.
 *
 * Format (must match Sherlocks exactly):
 *     base64url(payload) . "." . base64url(HMAC-SHA256(payload, secret))
 *     payload = {"u":"<label>","exp":<unix time>}   - compact JSON, no spaces
 *
 * "u" is only a label Sherlocks writes into its own log for each search; send whatever
 * identifies the officer to you (id, username), or leave it empty.
 */
class SherlocksToken
{
    public static function issue(string $user = '', ?int $ttl = null): string
    {
        $secret = config('sherlocks.secret');
        if (! $secret) {
            throw new RuntimeException('SHERLOCKS_SECRET is not set in .env');
        }

        $payload = json_encode(
            ['u' => $user, 'exp' => time() + ($ttl ?? config('sherlocks.token_ttl', 900))],
            JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE
        );

        return self::b64($payload) . '.' . self::b64(hash_hmac('sha256', $payload, $secret, true));
    }

    private static function b64(string $raw): string
    {
        return rtrim(strtr(base64_encode($raw), '+/', '-_'), '=');
    }
}
