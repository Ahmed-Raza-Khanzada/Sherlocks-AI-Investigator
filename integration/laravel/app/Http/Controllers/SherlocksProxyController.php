<?php

namespace App\Http\Controllers;

use GuzzleHttp\Client;
use GuzzleHttp\Exception\GuzzleException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Symfony\Component\HttpFoundation\StreamedResponse;

/**
 * Passes the Sherlocks tab's calls from the officer's browser on to the Sherlocks server.
 *
 * With this route the browser talks only to the portal's own domain (https, same origin,
 * no CORS), and only this Laravel server needs to reach Sherlocks on the internal network
 * (config sherlocks.api_base). Everything goes through it: the page's script and styles
 * (/portal/...), the API calls, file uploads, photos, PDFs and the live streams.
 *
 * Responses are passed on as they arrive, so the graph still grows live and chat replies
 * still stream. Each open stream keeps one PHP worker busy while it lasts (a live search:
 * a few minutes) - see README "Proxy through Laravel" for the PHP-FPM / nginx settings.
 */
class SherlocksProxyController extends Controller
{
    /** Request headers passed on to Sherlocks. Cookies (the portal's session) are not. */
    private const REQUEST_HEADERS = ['Accept', 'X-Session-Token', 'X-API-Key'];

    /** Response headers passed back to the browser. */
    private const RESPONSE_HEADERS = ['Content-Type', 'Content-Disposition', 'Cache-Control', 'ETag', 'Last-Modified'];

    public function __invoke(Request $request, string $path = ''): StreamedResponse|JsonResponse
    {
        $method = $request->method();
        $hasCredential = $request->hasHeader('X-Session-Token') || $request->hasHeader('X-API-Key');

        // Every call that changes something carries the Sherlocks token in a header. A
        // cross-site form cannot set one, which is why this route can be left out of the
        // CSRF check (README step 3).
        if (! in_array($method, ['GET', 'HEAD', 'OPTIONS'], true) && ! $hasCredential) {
            return response()->json(['detail' => 'Missing Sherlocks token'], 401);
        }

        $url = config('sherlocks.api_base') . '/' . ltrim($path, '/');
        if (($query = $request->getQueryString()) !== null) {
            $url .= '?' . $query;   // carries ?token= for the live stream and photos
        }

        $headers = ['X-Forwarded-For' => $request->ip()];
        foreach (self::REQUEST_HEADERS as $name) {
            if ($request->hasHeader($name)) {
                $headers[$name] = $request->header($name);
            }
        }

        $options = [
            'headers' => $headers,
            'stream' => true,             // pass the body on as it arrives (live graph, chat)
            'http_errors' => false,       // Sherlocks' own 4xx/5xx go back to the page as they are
            'connect_timeout' => 10,
            'timeout' => 0,               // a live search streams for minutes
            'read_timeout' => config('sherlocks.proxy_timeout', 900),
        ];

        if ($request->allFiles()) {
            // An upload (multipart). PHP has already parsed it, so build it again.
            $multipart = [];
            foreach ($request->request->all() as $name => $value) {
                $multipart[] = ['name' => $name, 'contents' => (string) $value];
            }
            foreach ($request->allFiles() as $name => $files) {
                foreach (is_array($files) ? $files : [$files] as $file) {
                    $multipart[] = [
                        'name' => $name,
                        'contents' => fopen($file->getRealPath(), 'r'),
                        'filename' => $file->getClientOriginalName(),
                        'headers' => ['Content-Type' => $file->getMimeType() ?: 'application/octet-stream'],
                    ];
                }
            }
            $options['multipart'] = $multipart;
        } elseif (! in_array($method, ['GET', 'HEAD'], true)) {
            $options['body'] = $request->getContent();
            $options['headers']['Content-Type'] = $request->header('Content-Type', 'application/json');
        }

        try {
            $upstream = (new Client())->request($method, $url, $options);
        } catch (GuzzleException $e) {
            report($e);
            return response()->json(['detail' => 'The Sherlocks server cannot be reached from the portal server'], 502);
        }

        $out = ['X-Accel-Buffering' => 'no'];   // nginx: do not hold the stream back
        foreach (self::RESPONSE_HEADERS as $name) {
            if ($upstream->hasHeader($name)) {
                $out[$name] = $upstream->getHeaderLine($name);
            }
        }

        $body = $upstream->getBody();

        return response()->stream(function () use ($body) {
            @set_time_limit(0);
            while (ob_get_level() > 0) {
                ob_end_flush();
            }
            while (! $body->eof()) {
                $chunk = $body->read(8192);
                if ($chunk !== '') {
                    echo $chunk;
                    flush();
                }
                if (connection_aborted()) {
                    break;   // the officer left the page: stop reading from Sherlocks
                }
            }
            $body->close();
        }, $upstream->getStatusCode(), $out);
    }
}
