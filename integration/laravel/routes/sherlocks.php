<?php

// Add to routes/web.php (or require this file from it). Put the group behind the same
// auth / permission middleware the rest of the portal uses.

use App\Http\Controllers\SherlocksController;
use Illuminate\Support\Facades\Route;

Route::middleware(['web', 'auth'])->prefix('sherlocks')->group(function () {
    Route::get('/', [SherlocksController::class, 'show'])->name('sherlocks.tab');                  // new search
    Route::get('/graphs/{graphId}', [SherlocksController::class, 'show'])->name('sherlocks.graph'); // reopen a saved graph
    Route::get('/token', [SherlocksController::class, 'token'])->name('sherlocks.token');
    Route::post('/graphs', [SherlocksController::class, 'store'])->name('sherlocks.graphs.store');
});
