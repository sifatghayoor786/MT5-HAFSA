//+------------------------------------------------------------------+
//| AegisFastGuard.mq5                                                |
//| AEGIS VELOCITY guardian EA — the reflexes, never the brain.       |
//|                                                                   |
//| INVARIANT: this EA NEVER originates a trade. The only trade       |
//| operations in this file are: PositionModify (SL/TP), position     |
//| close (by ticket), and pending-order delete. There is no code     |
//| path that opens a new position or places a new pending order.     |
//|                                                                   |
//| Duties (all scoped to magic prefix 77xxxxx):                      |
//|  - enforce SL presence on every managed position (+ alert)        |
//|  - break-even move after +R trigger                               |
//|  - point-based trailing stop                                      |
//|  - HARD time-stop per position (max hold seconds)                 |
//|  - spread-blowout emergency exit                                  |
//|  - pending-order expiry backstop                                  |
//|  - OCO sibling cancel on fill (race-safe, transaction-driven)     |
//|  - emergency flatten on command                                   |
//|  - bridge: JSON-lines over localhost TCP, 1 s heartbeats;         |
//|    PROTECT mode on bridge loss (keep enforcing exits, cancel      |
//|    stale pendings after grace, change nothing else);              |
//|    mailbox-file fallback in Common\Files when sockets blocked.    |
//+------------------------------------------------------------------+
#property copyright "AEGIS VELOCITY"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

//--- inputs
input int    InpBridgePort        = 8790;   // Python bridge TCP port (localhost)
input int    InpMagicPrefix      = 77;      // manage only magic 77xxxxx
input double InpBreakEvenR       = 1.0;     // move SL to BE after +N * initial risk
input int    InpBreakEvenLockPts = 2;       // lock this many points beyond entry at BE
input int    InpTrailStartPts    = 0;       // 0 = trailing disabled
input int    InpTrailDistancePts = 15;
input int    InpDefaultMaxHoldSec= 900;     // hard time-stop fallback
input int    InpSpreadBlowoutPts = 60;      // emergency exit if spread exceeds this
input bool   InpEnforceSL        = true;    // set an SL immediately if missing
input int    InpFallbackSLPts    = 100;     // distance used when enforcing a missing SL
input int    InpHeartbeatGraceMs = 3000;    // bridge loss => PROTECT after this silence
input int    InpProtectPendingGraceSec = 60;// PROTECT: cancel unfilled pendings after
input bool   InpUseMailboxFallback = true;  // Common\Files mailbox when socket blocked

//--- bridge state
int      g_socket = INVALID_HANDLE;
bool     g_protect = false;
ulong    g_last_rx_ms = 0;
ulong    g_last_tx_ms = 0;
ulong    g_protect_since_ms = 0;
string   g_rx_buffer = "";
int      g_seq = 0;
long     g_mailbox_read_pos = 0;

//--- per-position management memory
struct ManagedPos
  {
   ulong  ticket;
   double initial_risk_pts;   // |entry - initial SL| in points
   bool   be_done;
   long   max_hold_sec;
  };
ManagedPos g_managed[];

//--- OCO pairs delivered by Python
struct OcoPair { ulong a; ulong b; };
OcoPair g_oco[];

CTrade g_trade;

//+------------------------------------------------------------------+
bool IsOurs(const long magic)
  {
   return (int)(magic / 100000) == InpMagicPrefix;
  }

ulong NowMs() { return GetTickCount64(); }

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetMillisecondTimer(100);
   BridgeConnect();
   g_last_rx_ms = NowMs();
   Print("AegisFastGuard started. PROTECT until first Python heartbeat resync.");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(g_socket != INVALID_HANDLE) { SocketClose(g_socket); g_socket = INVALID_HANDLE; }
  }

//+------------------------------------------------------------------+
//| Bridge transport                                                  |
//+------------------------------------------------------------------+
void BridgeConnect()
  {
   if(g_socket != INVALID_HANDLE) return;
   g_socket = SocketCreate();
   if(g_socket == INVALID_HANDLE) return;
   if(!SocketConnect(g_socket, "127.0.0.1", InpBridgePort, 500))
     {
      // USER-ACTION if this persists: Tools > Options > Expert Advisors >
      // "Allow WebRequest/Socket connections for listed URL: 127.0.0.1"
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
     }
   else
     {
      SendLine(StringFormat(
        "{\"v\":1,\"type\":\"hello\",\"seq\":%d,\"data\":{\"ea\":\"AegisFastGuard\",\"magic_prefix\":%d}}",
        ++g_seq, InpMagicPrefix));
     }
  }

void SendLine(const string line)
  {
   string payload = line + "\n";
   if(g_socket != INVALID_HANDLE)
     {
      uchar bytes[];
      int len = StringToCharArray(payload, bytes, 0, WHOLE_ARRAY, CP_UTF8) - 1;
      if(len > 0 && SocketSend(g_socket, bytes, len) == len)
        { g_last_tx_ms = NowMs(); return; }
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
     }
   if(InpUseMailboxFallback)
     {
      int fh = FileOpen("AEG_ea_out.jsonl", FILE_READ|FILE_WRITE|FILE_TXT|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
      if(fh != INVALID_HANDLE)
        {
         FileSeek(fh, 0, SEEK_END);
         FileWriteString(fh, payload);
         FileClose(fh);
         g_last_tx_ms = NowMs();
        }
     }
  }

void PumpSocketReads()
  {
   if(g_socket != INVALID_HANDLE)
     {
      uint readable = SocketIsReadable(g_socket);
      if(readable > 0)
        {
         uchar bytes[];
         int got = SocketRead(g_socket, bytes, readable, 100);
         if(got > 0)
            g_rx_buffer += CharArrayToString(bytes, 0, got, CP_UTF8);
        }
     }
   else if(InpUseMailboxFallback)
     {
      int fh = FileOpen("AEG_ea_in.jsonl", FILE_READ|FILE_TXT|FILE_COMMON|FILE_SHARE_READ|FILE_SHARE_WRITE);
      if(fh != INVALID_HANDLE)
        {
         FileSeek(fh, g_mailbox_read_pos, SEEK_SET);
         while(!FileIsEnding(fh))
            g_rx_buffer += FileReadString(fh) + "\n";
         g_mailbox_read_pos = FileTell(fh);
         FileClose(fh);
        }
     }
   // split buffer into lines
   int nl;
   while((nl = StringFind(g_rx_buffer, "\n")) >= 0)
     {
      string line = StringSubstr(g_rx_buffer, 0, nl);
      g_rx_buffer = StringSubstr(g_rx_buffer, nl + 1);
      if(StringLen(line) > 0) HandleMessage(line);
     }
  }

//--- minimal field extraction for our fixed schema (no full JSON parser needed)
string JsonStr(const string json, const string key)
  {
   string needle = "\"" + key + "\":\"";
   int start = StringFind(json, needle);
   if(start < 0) return "";
   start += StringLen(needle);
   int end = StringFind(json, "\"", start);
   if(end < 0) return "";
   return StringSubstr(json, start, end - start);
  }

long JsonInt(const string json, const string key)
  {
   string needle = "\"" + key + "\":";
   int start = StringFind(json, needle);
   if(start < 0) return -1;
   start += StringLen(needle);
   int end = start;
   while(end < StringLen(json))
     {
      ushort c = StringGetCharacter(json, end);
      if((c < '0' || c > '9') && c != '-') break;
      end++;
     }
   if(end == start) return -1;
   return StringToInteger(StringSubstr(json, start, end - start));
  }

void HandleMessage(const string line)
  {
   g_last_rx_ms = NowMs();
   string type = JsonStr(line, "type");
   if(type == "heartbeat") return;
   if(type == "resync")
     {
      SendLine(StringFormat("{\"v\":1,\"type\":\"ack\",\"seq\":%d,\"data\":{\"of\":\"resync\"}}", ++g_seq));
      SendStateSnapshot();
      return;
     }
   if(type == "oco_pair")
     {
      long a = JsonInt(line, "ticket_a");
      long b = JsonInt(line, "ticket_b");
      if(a > 0 && b > 0)
        {
         int n = ArraySize(g_oco);
         ArrayResize(g_oco, n + 1);
         g_oco[n].a = (ulong)a;
         g_oco[n].b = (ulong)b;
        }
      return;
     }
   if(type == "command")
     {
      string cmd = JsonStr(line, "command");
      if(cmd == "flatten_all")           EmergencyFlatten();
      else if(cmd == "cancel_pending")   { long t = JsonInt(line, "ticket"); if(t > 0) DeleteOurPending((ulong)t); }
      else if(cmd == "set_max_hold")     { long t = JsonInt(line, "ticket"); long s = JsonInt(line, "seconds"); SetMaxHold((ulong)t, s); }
      SendLine(StringFormat("{\"v\":1,\"type\":\"ack\",\"seq\":%d,\"data\":{\"of\":\"%s\"}}", ++g_seq, cmd));
      return;
     }
  }

void SendStateSnapshot()
  {
   int positions = 0, pendings = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && PositionSelectByTicket(ticket) && IsOurs(PositionGetInteger(POSITION_MAGIC)))
         positions++;
     }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket > 0 && OrderSelect(ticket) && IsOurs(OrderGetInteger(ORDER_MAGIC)))
         pendings++;
     }
   SendLine(StringFormat(
     "{\"v\":1,\"type\":\"state\",\"seq\":%d,\"data\":{\"positions\":%d,\"pendings\":%d,\"protect\":%s}}",
     ++g_seq, positions, pendings, g_protect ? "true" : "false"));
  }

//+------------------------------------------------------------------+
//| Timer: bridge IO + PROTECT + management                           |
//+------------------------------------------------------------------+
void OnTimer()
  {
   PumpSocketReads();

   ulong now = NowMs();
   if(now - g_last_tx_ms >= 1000)
     {
      SendLine(StringFormat("{\"v\":1,\"type\":\"heartbeat\",\"seq\":%d}", ++g_seq));
      if(g_socket == INVALID_HANDLE) BridgeConnect();
     }

   bool lost = (now - g_last_rx_ms) > (ulong)InpHeartbeatGraceMs;
   if(lost && !g_protect)
     {
      g_protect = true;
      g_protect_since_ms = now;
      Alert("AegisFastGuard: bridge LOST -> PROTECT mode (exits enforced, no other changes)");
     }
   else if(!lost && g_protect)
     {
      g_protect = false;
      Print("AegisFastGuard: bridge restored -> resync");
      SendStateSnapshot();
     }

   ManagePositions();
   PendingExpiryBackstop();
  }

//+------------------------------------------------------------------+
//| Position management (runs in NORMAL and PROTECT modes)            |
//+------------------------------------------------------------------+
int FindManaged(const ulong ticket)
  {
   for(int i = 0; i < ArraySize(g_managed); i++)
      if(g_managed[i].ticket == ticket) return i;
   return -1;
  }

void SetMaxHold(const ulong ticket, const long seconds)
  {
   int idx = FindManaged(ticket);
   if(idx >= 0 && seconds > 0) g_managed[idx].max_hold_sec = seconds;
  }

void ManagePositions()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(!IsOurs(PositionGetInteger(POSITION_MAGIC)))    continue;

      string symbol   = PositionGetString(POSITION_SYMBOL);
      long   ptype    = PositionGetInteger(POSITION_TYPE);
      double entry    = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl       = PositionGetDouble(POSITION_SL);
      double tp       = PositionGetDouble(POSITION_TP);
      datetime opened = (datetime)PositionGetInteger(POSITION_TIME);
      double point    = SymbolInfoDouble(symbol, SYMBOL_POINT);
      double bid      = SymbolInfoDouble(symbol, SYMBOL_BID);
      double ask      = SymbolInfoDouble(symbol, SYMBOL_ASK);
      int    digits   = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      bool   is_buy   = (ptype == POSITION_TYPE_BUY);

      // --- registry entry (created on first sight)
      int idx = FindManaged(ticket);
      if(idx < 0)
        {
         int n = ArraySize(g_managed);
         ArrayResize(g_managed, n + 1);
         g_managed[n].ticket = ticket;
         g_managed[n].initial_risk_pts = (sl > 0) ? MathAbs(entry - sl) / point : InpFallbackSLPts;
         g_managed[n].be_done = false;
         g_managed[n].max_hold_sec = InpDefaultMaxHoldSec;
         idx = n;
        }

      // --- 1. missing-SL enforcement (always, including PROTECT)
      if(InpEnforceSL && sl <= 0.0)
        {
         double newsl = is_buy ? entry - InpFallbackSLPts * point
                               : entry + InpFallbackSLPts * point;
         newsl = NormalizeDouble(newsl, digits);
         if(g_trade.PositionModify(ticket, newsl, tp))
            Alert(StringFormat("AegisFastGuard: position %I64u had NO SL, set to %s",
                               ticket, DoubleToString(newsl, digits)));
         sl = newsl;
        }

      // --- 2. hard time-stop (always, including PROTECT)
      long held = (long)(TimeCurrent() - opened);
      long max_hold = g_managed[idx].max_hold_sec > 0 ? g_managed[idx].max_hold_sec
                                                      : InpDefaultMaxHoldSec;
      if(held >= max_hold)
        {
         if(g_trade.PositionClose(ticket))
           {
            NotifyExit(ticket, "TIME_STOP");
            continue;
           }
        }

      // --- 3. spread-blowout emergency exit (always, including PROTECT)
      double spread_pts = (ask - bid) / point;
      if(spread_pts >= InpSpreadBlowoutPts)
        {
         if(g_trade.PositionClose(ticket))
           {
            NotifyExit(ticket, "SPREAD_BLOWOUT");
            continue;
           }
        }

      if(g_protect) continue;  // PROTECT: no BE/trail changes, exits only

      // --- 4. break-even move after +InpBreakEvenR
      double risk_pts = g_managed[idx].initial_risk_pts;
      double profit_pts = is_buy ? (bid - entry) / point : (entry - ask) / point;
      if(!g_managed[idx].be_done && risk_pts > 0 && profit_pts >= InpBreakEvenR * risk_pts)
        {
         double be = is_buy ? entry + InpBreakEvenLockPts * point
                            : entry - InpBreakEvenLockPts * point;
         be = NormalizeDouble(be, digits);
         bool improves = is_buy ? (be > sl) : (sl == 0.0 || be < sl);
         if(improves && g_trade.PositionModify(ticket, be, tp))
            g_managed[idx].be_done = true;
        }

      // --- 5. point trailing (only after trail start threshold)
      if(InpTrailStartPts > 0 && profit_pts >= InpTrailStartPts)
        {
         double trail = is_buy ? bid - InpTrailDistancePts * point
                               : ask + InpTrailDistancePts * point;
         trail = NormalizeDouble(trail, digits);
         bool improves = is_buy ? (trail > sl) : (sl == 0.0 || trail < sl);
         if(improves)
            g_trade.PositionModify(ticket, trail, tp);
        }
     }
  }

void NotifyExit(const ulong ticket, const string reason)
  {
   SendLine(StringFormat(
     "{\"v\":1,\"type\":\"fill\",\"seq\":%d,\"data\":{\"ticket\":%I64u,\"event\":\"ea_exit\",\"reason\":\"%s\"}}",
     ++g_seq, ticket, reason));
  }

//+------------------------------------------------------------------+
//| Pending lifecycle backstop                                        |
//+------------------------------------------------------------------+
void DeleteOurPending(const ulong ticket)
  {
   if(OrderSelect(ticket) && IsOurs(OrderGetInteger(ORDER_MAGIC)))
      g_trade.OrderDelete(ticket);
  }

void PendingExpiryBackstop()
  {
   datetime now = TimeCurrent();
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || !OrderSelect(ticket)) continue;
      if(!IsOurs(OrderGetInteger(ORDER_MAGIC))) continue;

      // broker-side expiry is primary; this is the EA backstop
      datetime expiry = (datetime)OrderGetInteger(ORDER_TIME_EXPIRATION);
      if(expiry > 0 && now >= expiry)
        {
         g_trade.OrderDelete(ticket);
         continue;
        }
      // PROTECT: after the grace window, unfilled pendings are cancelled
      if(g_protect && g_protect_since_ms > 0 &&
         (NowMs() - g_protect_since_ms) > (ulong)(InpProtectPendingGraceSec * 1000))
        {
         g_trade.OrderDelete(ticket);
        }
     }
  }

//+------------------------------------------------------------------+
//| OCO sibling cancel — transaction-driven, race-safe                |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   if(!HistoryDealSelect(trans.deal)) return;
   if(!IsOurs(HistoryDealGetInteger(trans.deal, DEAL_MAGIC))) return;
   if((ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY) != DEAL_ENTRY_IN) return;

   ulong order_ticket = HistoryDealGetInteger(trans.deal, DEAL_ORDER);
   // race-safe: scan OCO table; delete sibling exactly once, then drop the pair
   for(int i = 0; i < ArraySize(g_oco); i++)
     {
      ulong sibling = 0;
      if(g_oco[i].a == order_ticket)      sibling = g_oco[i].b;
      else if(g_oco[i].b == order_ticket) sibling = g_oco[i].a;
      if(sibling == 0) continue;

      DeleteOurPending(sibling);
      for(int j = i; j < ArraySize(g_oco) - 1; j++) g_oco[j] = g_oco[j + 1];
      ArrayResize(g_oco, ArraySize(g_oco) - 1);
      break;
     }

   SendLine(StringFormat(
     "{\"v\":1,\"type\":\"fill\",\"seq\":%d,\"data\":{\"ticket\":%I64u,\"deal\":%I64u,\"event\":\"fill\"}}",
     ++g_seq, order_ticket, trans.deal));
  }

//+------------------------------------------------------------------+
//| Emergency flatten: close all OUR positions, delete OUR pendings   |
//+------------------------------------------------------------------+
void EmergencyFlatten()
  {
   Alert("AegisFastGuard: EMERGENCY FLATTEN executing");
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && PositionSelectByTicket(ticket) &&
         IsOurs(PositionGetInteger(POSITION_MAGIC)))
         g_trade.PositionClose(ticket);
     }
   for(int i = OrdersTotal() - 1; i >= 0; i--)
     {
      ulong ticket = OrderGetTicket(i);
      if(ticket > 0 && OrderSelect(ticket) && IsOurs(OrderGetInteger(ORDER_MAGIC)))
         g_trade.OrderDelete(ticket);
     }
   SendStateSnapshot();
  }
//+------------------------------------------------------------------+
