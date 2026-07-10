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
                // v counts EVERY event (clockInc, chat, moretime...) so its
                // parity drifts; ply counts only moves. Fall back to v if absent.
                const ply = typeof msg.d.ply === 'number' ? msg.d.ply : msg.v;
                currentSide = ply % 2 === 0 ? 'w' : 'b';
                currentMoveNumber = Math.floor(ply / 2) + 1;  // whole moves only
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

function startSearch() {
    const fenAtRequest = currentFen;
    const moveNumAtRequest = currentMoveNumber;
    const pieces = (currentFen.split(' ')[0].match(/[a-zA-Z]/g) || []).length;
    const depth = Math.min(18, 12 + Math.floor((32 - pieces) / 4));
    inFlight = true;

    GM_xmlhttpRequest({
        method: 'POST',
        url: BRIDGE_URL,
        data: JSON.stringify({ fen: fenAtRequest, depth: depth }),
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
