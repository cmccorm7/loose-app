#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using System.Text;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

// YM Desk - bar feed via file
//
// Appends every closed bar to a CSV that YM Desk watches. Nothing is sent over
// the network, and it does not care whether the app is running: the file is the
// buffer, and the app catches up when it next reads.
//
// Install
//   1. NinjaTrader: New > NinjaScript Editor
//   2. Right-click Indicators > New Indicator, name it anything, Finish
//   3. Select all the generated code, paste this over it, press F5 to compile
//   4. Put it on an MYM 1-minute chart (right-click > Indicators > YmDeskBarFile)
//
// The file lands in  %USERPROFILE%\.ym-desk\live\  next to the app's own data,
// one file per instrument and period, e.g. MYM_1min.csv
//
// Timestamps are written in NinjaTrader's configured timezone with no offset,
// so the app's "Statement timezone" setting must match NinjaTrader's. Getting
// that wrong shifts every session boundary silently.

namespace NinjaTrader.NinjaScript.Indicators
{
    public class YmDeskBarFile : Indicator
    {
        private string filePath;
        private int barsWritten;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name                        = "YmDeskBarFile";
                Description                 = "Appends closed bars to a CSV for YM Desk.";
                Calculate                   = Calculate.OnBarClose;
                IsOverlay                   = true;
                DisplayInDataBox            = false;
                DrawOnPricePanel            = false;
                PaintPriceMarkers           = false;
                IsSuspendedWhileInactive    = false;

                OutputFolder     = "";
                IncludeHistorical = false;
            }
            else if (State == State.DataLoaded)
            {
                PrepareFile();
            }
            else if (State == State.Terminated)
            {
                if (barsWritten > 0)
                    Print(string.Format("YmDeskBarFile: wrote {0} bars to {1}",
                                        barsWritten, filePath));
            }
        }

        // ------------------------------------------------------------------

        private string ResolveFolder()
        {
            if (!string.IsNullOrWhiteSpace(OutputFolder))
                return OutputFolder;

            // Beside the app's own data, so one folder holds everything.
            string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            return Path.Combine(home, ".ym-desk", "live");
        }

        private void PrepareFile()
        {
            try
            {
                string folder = ResolveFolder();
                Directory.CreateDirectory(folder);

                string period = string.Format(
                    "{0}{1}",
                    BarsPeriod.Value,
                    BarsPeriod.BarsPeriodType == BarsPeriodType.Minute ? "min" :
                    BarsPeriod.BarsPeriodType.ToString().ToLowerInvariant());

                filePath = Path.Combine(
                    folder,
                    string.Format("{0}_{1}.csv", Instrument.MasterInstrument.Name, period));

                // Header only on a new or empty file, so restarting the
                // indicator does not scatter headers through the data.
                if (!File.Exists(filePath) || new FileInfo(filePath).Length == 0)
                    File.AppendAllText(
                        filePath,
                        "symbol,datetime,open,high,low,close,volume" + Environment.NewLine,
                        Encoding.UTF8);
            }
            catch (Exception error)
            {
                Print("YmDeskBarFile: could not prepare the output file - " + error.Message);
                filePath = null;
            }
        }

        protected override void OnBarUpdate()
        {
            if (filePath == null || BarsInProgress != 0 || CurrentBar < 1)
                return;

            // Historical bars are only written when asked for, so attaching the
            // indicator does not replay months of data every time.
            if (State == State.Historical && !IncludeHistorical)
                return;

            try
            {
                string line = string.Join(",", new string[]
                {
                    Instrument.MasterInstrument.Name,
                    Time[0].ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture),
                    Open[0].ToString(CultureInfo.InvariantCulture),
                    High[0].ToString(CultureInfo.InvariantCulture),
                    Low[0].ToString(CultureInfo.InvariantCulture),
                    Close[0].ToString(CultureInfo.InvariantCulture),
                    ((long)Volume[0]).ToString(CultureInfo.InvariantCulture)
                });

                File.AppendAllText(filePath, line + Environment.NewLine, Encoding.UTF8);
                barsWritten++;
            }
            catch (Exception error)
            {
                // A locked or full disk must not take the chart down with it.
                Print("YmDeskBarFile: could not write a bar - " + error.Message);
            }
        }

        // ------------------------------------------------------------------

        [Display(Name = "Output folder", Order = 1, GroupName = "YM Desk",
                 Description = "Leave blank for %USERPROFILE%\\.ym-desk\\live")]
        public string OutputFolder { get; set; }

        [Display(Name = "Include historical bars", Order = 2, GroupName = "YM Desk",
                 Description = "Write the chart's existing bars too, to seed the app once")]
        public bool IncludeHistorical { get; set; }
    }
}
