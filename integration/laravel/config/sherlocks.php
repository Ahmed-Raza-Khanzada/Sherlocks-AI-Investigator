<?php

// Sherlocks - the link-graph service this portal embeds as a tab.
// Values come from .env (see integration/laravel/env.example).

return [
    // Where Sherlocks is. With the proxy on (the default) only THIS SERVER calls this
    // address; officers' browsers call /sherlocks/api on the portal's own domain.
    'api_base' => rtrim(env('SHERLOCKS_API_BASE', 'http://192.168.200.239:7401/sindhpolice-sherlocks'), '/'),

    // true: the tab goes through the portal (SherlocksProxyController) - needed when the
    // portal is on a public or https domain and officers' browsers cannot reach
    // api_base. false: browsers call api_base directly (same network, same scheme).
    'proxy' => filter_var(env('SHERLOCKS_PROXY', true), FILTER_VALIDATE_BOOLEAN),

    // Seconds the proxy waits for the next bytes from Sherlocks (a report rebuild can be
    // quiet for a few minutes; live streams send a keep-alive every 15 s).
    'proxy_timeout' => (int) env('SHERLOCKS_PROXY_TIMEOUT', 900),

    // Shared with the Sherlocks server (its SHERLOCKS_AUTH_SECRET). Signs the short-lived
    // tokens the browser uses. Never put it in a page or a repository.
    'secret' => env('SHERLOCKS_SECRET'),

    // Token lifetime in seconds. The page asks for a new one when it runs out.
    'token_ttl' => (int) env('SHERLOCKS_TOKEN_TTL', 900),
];
