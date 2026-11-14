"""Owner-facing Hinglish messages (code and technical text stay in English)."""

TOKEN_EXPIRED = "Token expire ho gaya. Naya token banao aur wahi command dobara chalao. Kaam wahin se aage badhega."
DATA_RIGHTS_WARNING = "Data sirf apne use ke liye hai. Kisi ko mat do. Upstox se likhit jawab lo."
TOKEN_MISSING = ("Token nahi mila. UPSTOX_ANALYTICS_TOKEN (ya UPSTOX_ACCESS_TOKEN, ya UPSTOX_TOKEN_FILE) set karo, "
                 "phir wahi command dobara chalao.")
PLAN_A_PARTIAL = ("Expired data poore 6 mahine peeche tak nahi hai. Window chhoti ho gayi. "
                  "Aage ke liye market time mein Recorder chalao: python -m updl record")
PLAN_B = ("Expired option history nahi mili (Upstox Plus chahiye). Aage ke liye market time mein "
          "Recorder chalao: python -m updl record")
PROBE_TOKEN_BLOCKED = ("Probe token ki wajah se adhura raha (token nahi mila ya reject hua). Token theek karke "
                       "`python -m updl probe` dobara chalao.")
ROUTING_SUSPECTED = ("5 baar lagataar Upstox ne key reject ki (UDAPI1021 = key galat endpoint par gayi). Yeh program ka "
                     "routing bug lagta hai, isliye calls barbaad karne ki jagah ruk gaya. State safe hai. "
                     "Code update karo (bash termux_run.sh update), phir: bash termux_run.sh repair")
ALREADY_RUNNING = ("Is folder par pehle se ek updl chal raha hai ({command}, PID {pid}, {started} baje se). Do run ek "
                   "saath state kharab kar sakte hain. Us run ko khatam hone do. Agar woh sach mein band ho chuka "
                   "hai to yeh file hata do: {path}")
STATE_DB_BROKEN = ("State database kharab mila (shayad phone beech mein band hua): {path}. Kuch download nahi kiya. "
                   "Yeh file mujhe (AI ko) batao; ise khud delete mat karo, warna dobara sab download hoga.")
REPAIR_HINT = ("{n} chunks purane galat-endpoint error (UDAPI1021) se fail pade hain. Unhe wapas line mein lagane ke "
               "liye chalao: bash termux_run.sh repair")
SDCARD_HINT = ("Phone ke Download folder mein copy nahi ho paya. Termux mein ek baar `termux-setup-storage` "
               "chalao aur Allow dabao. Zip abhi bhi yahan hai:")
PLAN_C = "Plan C se idea reject ho sakta hai, approve nahi."
PROBE_MISSING = ("probe_report.json nahi mila ya 7 din se purana hai ya usme secret mila. "
                 "Pehle `python -m updl probe` chalao.")
RIGHTS_NOT_ALLOWED = "DATA_RIGHTS = NOT_ALLOWED hai. Kuch download nahi hoga."
KEY_AMBIGUOUS = "Spot key ek se zyada mil rahi hain. data_manifest.yaml mein `spot_key` khud likho."
NO_REAL_DATA = ("Yeh code ne koi asli data nahi utaara. Asli data owner ki machine par "
                "probe aur download chalane ke baad hi milega.")
CTRL_C = "Ctrl+C mila. Current call ke baad rukk raha hoon. State safe hai, wahi command dobara chalao."
CORRUPT = "Raw file kharab mili (sha256 match nahi hua). File ka naam neeche hai."
HOLDOUT_REFUSED = "Holdout band hai. Dev data hi khulega."
HOLDOUT_USED = "Holdout pehle hi ek baar khul chuka hai. Dobara nahi khulega."
RATE_PAUSE = "Teen baar 429 mila. 10 minute ruk kar aadhi speed par chalunga."
HOLIDAY_TABLE_END = "holidays.yaml 2026-12-31 ke baad ka data nahi rakhti. Weekdays use ho rahe hain. Table update karo."
VERIFY_OK = "Sab files ka sha256 match ho gaya."
VERIFY_BAD = "Neeche likhi files badal gayi ya gayab hain."
