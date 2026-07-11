// ==UserScript==
// @name         Lichess Bot Pygin
// @description  Fully automated lichess bot using local pygin engine
// @author       Nuro
// @match         *://lichess.org/*
// @run-at        document-start
// @grant         GM_xmlhttpRequest
// @grant         unsafeWindow
// @connect       127.0.0.1
// ==/UserScript==

const BRIDGE_URL = 'http://127.0.0.1:8118';

let webSocketWrapper = null;
let myColor = null;          // 'w' or 'b' — which side we are playing

let currentFen = "";
let currentSide = 'w';       // side to move for currentFen
let currentMoveNumber = 1;   // whole-move number for currentFen
let currentPly = 0;          // ply count for currentFen
let moveHistory = [];        // moveHistory[ply-1] = uci of that move

let inFlight = false;        // is a request to the bridge running?

interceptWebSocket();
kickoffAsWhite();

function interceptWebSocket() {
    const OrigWebSocket = unsafeWindow.WebSocket;
    unsafeWindow.WebSocket = new Proxy(OrigWebSocket, {
        construct: function(target, args) {
            const ws = new target(...args);
            webSocketWrapper = ws;
            ws.addEventListener('message', function(e) {
                const msg = JSON.parse(e.data);
                if (msg.t !== 'move' || !msg.d || typeof msg.d.fen !== 'string') return;
                // Track ply ourselves by counting move events in order — don't
                // rely on d.ply (not always present) or v (counts clock/chat
                // events too, so its parity drifts). Contiguous history for a
                // game watched from the start; a gap only if we joined midway.
                if (msg.d.uci) {
                    const ply = typeof msg.d.ply === 'number' ? msg.d.ply
                                                              : moveHistory.length + 1;
                    moveHistory[ply - 1] = msg.d.uci;
                    currentPly = ply;
                }
                currentSide = currentPly % 2 === 0 ? 'w' : 'b';
                currentMoveNumber = Math.floor(currentPly / 2) + 1;  // whole moves only
                currentFen = msg.d.fen + ' ' + currentSide;
                maybeSearch();
            });
            return ws;
        }
    });
}

// Read which colour we're playing from the board orientation.
function detectColor() {
    if (myColor) return myColor;
    const wrap = document.querySelector('.cg-wrap');
    if (wrap) {
        if (wrap.classList.contains('orientation-white')) myColor = 'w';
        else if (wrap.classList.contains('orientation-black')) myColor = 'b';
    }
    return myColor;
}

function maybeSearch() {
    const color = detectColor();
    if (color && currentSide !== color) return; // not our turn — skip
    if (inFlight) return; // response handler re-checks currentFen and re-fires
    startSearch();
}

// Full move list if we have every ply contiguously (so the engine sees
// repetitions); null if there's a gap (e.g. joined mid-game) → caller uses FEN.
function movesString() {
    for (let i = 0; i < currentPly; i++) {
        if (moveHistory[i] === undefined) return null;
    }
    return moveHistory.slice(0, currentPly).join(' ');
}

function startSearch() {
    const fenAtRequest = currentFen;
    const moveNumAtRequest = currentMoveNumber;
    const pieces = (currentFen.split(' ')[0].match(/[a-zA-Z]/g) || []).length;
    const depth = Math.min(18, 12 + Math.floor((32 - pieces) / 4));
    const moves = movesString();
    const payload = moves !== null ? { moves: moves, depth: depth }
                                    : { fen: fenAtRequest, depth: depth };
    console.log('[Bot]', moves !== null ? 'history: ' + moves : 'FEN: ' + fenAtRequest);
    inFlight = true;

    GM_xmlhttpRequest({
        method: 'POST',
        url: BRIDGE_URL,
        data: JSON.stringify(payload),
        headers: { 'Content-Type': 'application/json' },
        onload: function(response) {
            inFlight = false;
            if (response.status !== 200) {
                console.error('[Bot] Bridge HTTP', response.status);
                return;
            }
            // Position changed while we were searching — result is stale,
            // search the latest position instead.
            if (currentFen !== fenAtRequest) {
                maybeSearch();
                return;
            }
            const move = JSON.parse(response.responseText).bestmove;
            if (move && move !== '(none)') {
                console.log('[Bot] Playing:', move, 'move', moveNumAtRequest);
                webSocketWrapper.send(JSON.stringify({
                    t: 'move',
                    d: { u: move, s: '0', a: String(moveNumAtRequest) }
                }));
            }
        },
        onerror: function() {
            inFlight = false;
            console.error('[Bot] Bridge unreachable — is pygin_server.py running?');
        }
    });
}

// As White no 'move' event fires for the opening position; seed it ourselves.
function kickoffAsWhite() {
    let tries = 0;
    const timer = setInterval(function() {
        if (currentFen !== '' || ++tries > 40) { clearInterval(timer); return; }
        const color = detectColor();
        if (color === 'b') { clearInterval(timer); return; }
        if (color === 'w') {
            clearInterval(timer);
            currentSide = 'w';
            currentMoveNumber = 1;
            currentFen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
            maybeSearch();
        }
    }, 250);
}
