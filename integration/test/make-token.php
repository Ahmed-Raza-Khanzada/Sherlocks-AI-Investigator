<?php

// Makes a test token and prints the commands to check it against Sherlocks - before any
// Laravel code exists. Same algorithm as app/Support/SherlocksToken.php.
//
//   php make-token.php "<SHERLOCKS_SECRET>" [http://192.168.200.239:7401/sindhpolice-sherlocks]

$secret = $argv[1] ?? '';
$base = rtrim($argv[2] ?? 'http://192.168.200.239:7401/sindhpolice-sherlocks', '/');
if ($secret === '') {
    fwrite(STDERR, "usage: php make-token.php \"<SHERLOCKS_SECRET>\" [api_base]\n");
    exit(1);
}

$b64 = fn (string $s) => rtrim(strtr(base64_encode($s), '+/', '-_'), '=');
$payload = json_encode(['u' => 'integration-test', 'exp' => time() + 3600], JSON_UNESCAPED_SLASHES);
$token = $b64($payload) . '.' . $b64(hash_hmac('sha256', $payload, $secret, true));

echo "TOKEN (valid 1 hour):\n$token\n\n";
echo "1) Is the token accepted? Expect \"authenticated\":true\n";
echo "   curl -s -H 'X-Session-Token: $token' '$base/auth/session'\n\n";
echo "2) Can the API be used? Expect a JSON with \"systems\"\n";
echo "   curl -s -H 'X-Session-Token: $token' '$base/graph/config?backend=ems' | head -c 300\n\n";
echo "3) Open test/sherlocks-test.html in a browser after pasting the token into it.\n";
