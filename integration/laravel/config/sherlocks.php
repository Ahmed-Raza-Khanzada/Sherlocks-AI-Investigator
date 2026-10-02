<?php

// Sherlocks - the link-graph service this portal embeds as a tab.
// Values come from .env (see integration/laravel/env.example).

return [
    // Where officers' BROWSERS reach Sherlocks (not just this server): the API and the
    // page's script/styles are loaded from here.
    'api_base' => rtrim(env('SHERLOCKS_API_BASE', 'http://192.168.200.239:7401/sindhpolice-sherlocks'), '/'),

    // Shared with the Sherlocks server (its SHERLOCKS_AUTH_SECRET). Signs the short-lived
    // tokens the browser uses. Never put it in a page or a repository.
    'secret' => env('SHERLOCKS_SECRET'),

    // Token lifetime in seconds. The page asks for a new one when it runs out.
    'token_ttl' => (int) env('SHERLOCKS_TOKEN_TTL', 900),
];
