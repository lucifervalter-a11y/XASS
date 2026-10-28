using Xass.Native.Services;

int checks = 0;
void Require(bool condition, string label) { checks++; if (!condition) throw new InvalidOperationException(label); }
foreach(var bytes in new[] { Array.Empty<byte>(), new byte[3], new byte[256*256*4+4], new byte[] {1,2,3,0} })
    Require(CoverPalette.FromBgra(bytes) is null,"empty, malformed, oversized or transparent pixels produce no palette");
var palette = CoverPalette.FromBgra(new byte[] { 20,40,240,255, 22,42,242,255, 230,160,20,255, 0,255,0,20 });
Require(palette == new CoverPalette(0xF12915,0x14A0E6),"dominant opaque BGRA average plus distinct secondary");
Require(CoverPalette.FromBgra(new byte[] {10,20,30,255}) == new CoverPalette(0x1E140A,0x1E140A),"single-color artwork fallback");
Require(CoverPalette.Blend(0xFFFFFF,0x000000,-1)==0,"negative strength clamped");
Require(CoverPalette.Blend(0xFFFFFF,0x000000,2)==0xFFFFFF,"excess strength clamped");
Require(CoverPalette.Blend(0xFF0000,0x0000FF,.5)==0x800080,"channel blend");
var history = new MusicSessionHistory(); var now = DateTimeOffset.Parse("2026-10-07T20:00:00Z");
foreach(var state in new[] {"idle","loading","paused","ended","error","unavailable"})
    Require(!history.Observe(state,"a","Track","Artist",now),"only observed playback enters history");
Require(!history.Observe("playing","","Track","Artist",now),"requires identity");
Require(!history.Observe("playing","a"," ","Artist",now),"requires actual title");
Require(history.Observe("playing","a","Track","Artist",now),"first playback");
Require(!history.Observe("playing","a","Track","Artist",now.AddSeconds(2)),"poll deduplication");
history.Observe("paused","a","Track","Artist",now);
Require(!history.Observe("playing","a","Track","Artist",now),"pause/resume does not inflate history");
Require(history.Observe("playing","b","Second","",now),"different track");
Require(history.Entries[0].Title=="Second" && history.Entries[1].Artist=="Artist","newest first, actual metadata retained");
Require(!history.Entries[0].Display.Contains("·"),"missing artist is not fabricated");
Require(history.Observe("playing","a","Track","Artist",now),"returning to an earlier track is recorded");
for(int i=0;i<60;i++) history.Observe("playing","track-"+i,"Title "+i,"",now.AddMinutes(i));
Require(history.Entries.Count==50 && history.Entries[0].Title=="Title 59" && history.Entries[^1].Title=="Title 10","bounded real session history");
Require(new MusicSessionHistory().Entries.Count==0,"history is not fabricated or persisted across sessions");
Console.WriteLine($"Music presentation tests passed: {checks} assertions.");
