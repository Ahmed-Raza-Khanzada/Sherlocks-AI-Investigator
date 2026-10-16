{{--
    Sherlocks tab - the live link graph + AI chat, served by the Sherlocks server.

    This page holds only the portal's own part: the config below (token, saving). The
    Sherlocks page itself - markup, styles, scripts - is loaded from the Sherlocks server by
    embed.js when the tab opens, so a change on the Sherlocks side shows up here without
    touching this file.

    A full page on purpose: Sherlocks' stylesheet styles html/body and uses class names
    (.card, .row, .badge) that Bootstrap/Tailwind layouts also use. Show this page from
    the portal's menu, or inside the portal layout with a same-site iframe (README step 4).

    Variables (from SherlocksController::show): $sherlocksApiBase, $sherlocksAssets,
    $sherlocksToken, $tokenUrl, $saveUrl (nullable), $savedRun (nullable array).
--}}
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sherlocks · Link Graph</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='9' cy='9' r='5' fill='%23fbbf24'/%3E%3Ccircle cx='24' cy='12' r='4' fill='%2338bdf8'/%3E%3Ccircle cx='14' cy='25' r='4' fill='%23f43f5e'/%3E%3Cpath d='M9 9L24 12M9 9L14 25' stroke='%23e5e7eb' stroke-width='2'/%3E%3C/svg%3E">
  <meta name="csrf-token" content="{{ csrf_token() }}">
</head>
<body>
  <div id="sherlocksRoot"></div>

  <script>
    function saveRun(run) {
      const saveUrl = @json($saveUrl ?? null);
      if (!saveUrl) return;
      fetch(saveUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json',
                   'X-CSRF-TOKEN': document.querySelector('meta[name=csrf-token]').content },
        body: JSON.stringify(run),
      }).catch((err) => console.error('Saving the Sherlocks graph failed', err));
    }

    window.SHERLOCKS_CONFIG = {
      embedded: true,                 // no Sherlocks sign-in / history / key buttons - the portal owns those
      backend: 'ems',                 // live police systems only (no Demo option in the portal)
      apiBase: @json($sherlocksApiBase),
      token: @json($sherlocksToken),
      savedRun: @json($savedRun ?? null),   // a graph the portal saved earlier, or null for a new search
      // theme: 'dark',                     // optional: start officers in dark (default light)

      // A search finished: `run` is the whole graph (nodes, edges, params, stats, log, and
      // graph.case - the case file: documents, facts, uploads, incident, questions, report).
      // Saving it is the portal's job - here it is POSTed to $saveUrl if one is given.
      onRunComplete(run) { saveRun(run); },

      // The case of a finished search changed: an uploaded file was read, the incident was
      // pinned on the map, a question was answered, the chat fetched a document. Save again
      // (same run id: the controller updates the saved row).
      onCaseUpdated(run) { saveRun(run); },

      onRunStarted(runId) { /* optional: e.g. show "searching..." in the portal */ },

      // Tokens are short-lived; when one expires Sherlocks asks for a fresh one here.
      async onTokenExpired() {
        const r = await fetch(@json($tokenUrl), { headers: { 'Accept': 'application/json' } });
        return r.ok ? (await r.json()).token : null;
      },
    };
  </script>
  <script src="{{ $sherlocksAssets }}/embed.js"></script>
</body>
</html>
