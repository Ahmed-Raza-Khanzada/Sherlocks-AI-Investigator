<?php

namespace App\Http\Controllers;

use App\Support\SherlocksToken;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\View\View;

/**
 * The Sherlocks tab.
 *
 * Sherlocks (the separate server) builds the graph live and answers the chat. This
 * controller only: shows the page, hands the browser a token, and - optionally - saves
 * finished graphs and reopens them. How graphs are stored, and who may see which, is
 * entirely the portal's decision; the two TODOs below are where that goes.
 */
class SherlocksController extends Controller
{
    /** New search (no $graphId) or a saved graph reopened (chat works on it too). */
    public function show(Request $request, ?string $graphId = null): View
    {
        $savedRun = null;
        if ($graphId !== null) {
            // TODO (portal): load the saved graph by id - with your own permission check -
            // and decode the JSON you stored in store() back to an array, e.g.:
            //   $savedRun = json_decode(SherlocksGraph::findOrFail($graphId)->payload, true);
            abort_if($savedRun === null, 404);
        }

        return view('sherlocks.tab', [
            'sherlocksApiBase' => config('sherlocks.api_base'),
            'sherlocksAssets' => config('sherlocks.api_base') . '/portal',
            'sherlocksToken' => SherlocksToken::issue((string) optional($request->user())->getAuthIdentifier()),
            'tokenUrl' => route('sherlocks.token'),
            'saveUrl' => route('sherlocks.graphs.store'),   // set to null to not save at all
            'savedRun' => $savedRun,
        ]);
    }

    /** A fresh token when the page's one expires (called by the page itself). */
    public function token(Request $request): JsonResponse
    {
        return response()->json([
            'token' => SherlocksToken::issue((string) optional($request->user())->getAuthIdentifier()),
        ]);
    }

    /** A finished search, POSTed by the page: the whole run as JSON. */
    public function store(Request $request): JsonResponse
    {
        $run = $request->json()->all();   // keys: id, seed_label, params, stats, graph{nodes,edges}, events

        // TODO (portal): store it however you like, e.g. one row per graph:
        //   SherlocksGraph::create([
        //       'user_id'  => $request->user()->id,
        //       'run_id'   => $run['id'] ?? null,
        //       'subject'  => $run['seed_label'] ?? null,
        //       'payload'  => json_encode($run),        // longText / json column
        //   ]);
        // Reopen it later with route('sherlocks.graph', $id).

        return response()->json(['saved' => true]);
    }
}
