#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.Net.Http;
using System.Text;
using System.Threading.Tasks;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

// YM Desk - bar feed via localhost
//
// Posts every closed bar straight to YM Desk. Lower latency than the file
// version, but anything produced while the app is closed is simply lost -- the
// post fails and is ignored. Use this one if you always have the app open, or
// if you later want live events rather than only bars.
//
// NinjaTrader treats outbound HTTP from NinjaScript as outside officially
// supported use. It works and is widely done, but that is worth knowing.
//
// Install
//   1. NinjaTrader: New > NinjaScript Editor
//   2. Right-click Indicators > New Indicator, name it anything, Finish
//   3. Select all the generated code, paste this over it, press F5 to compile
//   4. Put it on an MYM 1-minute chart
//
// Timestamps are sent in NinjaTrader's configured timezone with no offset, so
// the app's "Statement timezone" setting must match NinjaTrader's.

namespace NinjaTrader.NinjaScript.Indicators
{
    public class YmDeskBarHttp : Indicator
    {
        // One client for the life of the process. A new HttpClient per request
        // exhausts sockets, which is the classic way to break a long-running
        // feed a few hours in.
        private static readonly HttpClient Client = BuildClient();

        private int barsSent;
        private int failures;
        private bool warned;

        private static HttpClient BuildClient()
        {
            HttpClient client = new HttpClient();
            // Short, because a hung app must not queue up behind itself.
            client.Timeout = TimeSpan.FromSeconds(2);
            return client;
        }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name                        = "YmDeskBarHttp";
                Description                 = "Posts closed bars to YM Desk on localhost.";
                Calculate                   = Calculate.OnBarClose;
                IsOverlay                   = true;
                DisplayInDataBox            = false;
                DrawOnPricePanel            = false;
                PaintPriceMarkers           = false;
                IsSuspendedWhileInactive    = false;

                Endpoint = "http://127.0.0.1:8787/api/live/bar";
            }
            else if (State == State.Terminated)
            {
                if (barsSent > 0 || failures > 0)
                    Print(string.Format("YmDeskBarHttp: {0} bars sent, {1} failed",
                                        barsSent, failures));
            }
        }

        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0 || CurrentBar < 1)
                return;

            // Historical bars are never posted: seeding months of data one
            // request at a time is what the file feed is for.
            if (State != State.Realtime)
                return;

            string payload = string.Format(
                CultureInfo.InvariantCulture,
                "{{\"symbol\":\"{0}\",\"datetime\":\"{1}\",\"open\":{2},\"high\":{3}," +
                "\"low\":{4},\"close\":{5},\"volume\":{6},\"minutes\":{7}}}",
                Instrument.MasterInstrument.Name,
                Time[0].ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture),
                Open[0].ToString(CultureInfo.InvariantCulture),
                High[0].ToString(CultureInfo.InvariantCulture),
                Low[0].ToString(CultureInfo.InvariantCulture),
                Close[0].ToString(CultureInfo.InvariantCulture),
                ((long)Volume[0]).ToString(CultureInfo.InvariantCulture),
                BarsPeriod.Value);

            Post(payload);
        }

        private void Post(string json)
        {
            // Fire and forget. Never await here: awaiting on NinjaTrader's
            // thread is how charts freeze.
            try
            {
                StringContent content = new StringContent(json, Encoding.UTF8, "application/json");
                Client.PostAsync(Endpoint, content).ContinueWith(task =>
                {
                    if (task.IsFaulted || task.IsCanceled)
                    {
                        // Observe the exception so it is not rethrown on the
                        // finalizer thread, then carry on: the app being shut
                        // is the normal case, not an error worth shouting about.
                        if (task.Exception != null)
                        {
                            AggregateException ignored = task.Exception.Flatten();
                            GC.KeepAlive(ignored);
                        }
                        failures++;
                        if (!warned)
                        {
                            warned = true;
                            Print("YmDeskBarHttp: cannot reach " + Endpoint +
                                  " - is YM Desk running? Bars sent while it is " +
                                  "closed are lost. Further failures stay quiet.");
                        }
                    }
                    else
                    {
                        barsSent++;
                        warned = false;
                        // Only safe here: reading Result on a faulted task
                        // rethrows, which would break the continuation itself.
                        if (task.Result != null)
                            task.Result.Dispose();
                    }
                }, TaskContinuationOptions.ExecuteSynchronously);
            }
            catch (Exception error)
            {
                failures++;
                Print("YmDeskBarHttp: " + error.Message);
            }
        }

        [Display(Name = "Endpoint", Order = 1, GroupName = "YM Desk",
                 Description = "Where YM Desk is listening. Check the port in the app's window.")]
        public string Endpoint { get; set; }
    }
}
