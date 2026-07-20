# Bug report

## Findings

### 01_new_appointment

**Bug**: Agent does not honor Spanish-language selection from IVR menu
**Severity**: **Medium-High**
**Call**: transcript-01.txt at 00:06-00:10
**Details**: Agent offered "Para español, oprima el dos" and the caller selected 
the Spanish option ("Presiono el 2 para Español"). The agent's next response 
("Thank you for calling Pivot Point Orthopedics") was in English, and the 
call proceeded in English for its entirety despite the caller's explicit 
language selection. A real Spanish-speaking patient selecting this option 
would not be served in their chosen language.

**Bug**: Agent gives three different spellings of the assigned doctor's name within a single call
**Severity**: **Medium**
**Call**: transcript-01.txt at 01:18, 02:02, 02:31
**Details**: Agent refers to the same doctor as "Dr. Zbigniew Lekowski" (01:18, 02:02) 
then "Dr. Zbigniew Lukasik" (02:31) in its own final confirmation. Patient could 
reasonably write down the wrong name, causing confusion at check-in or when 
looking up the provider for insurance verification.

**Bug**: Agent assigns a fabricated "demo" date of birth instead of asking the caller for their real one, and the same patient ends up with two different DOBs across calls
**Severity**: **High**
**Call**: transcript-01.txt (CA37aa13d1b13281017b20686ce4fcc046) at 00:35, contradicted by 
transcript-01.txt (CAfd393472b05fe634a7d014e44a9867c4) at 00:30
**Details**: In this call the agent never asks the caller for a date of birth -- it says 
"Your patient profile is set up and your date of birth is July 4th, 2000 for demo purposes," 
inventing a DOB unprompted. In a separate call under the same scenario and caller identity 
("Maria Chen"), the agent instead explicitly asks "Please provide your date of birth," and 
the caller states "March 14th, 1985." The same named patient now has two contradictory 
DOBs on record depending on which path the agent happened to take. In a real clinic this is 
a patient-identity integrity problem (DOB is a standard identity/lookup field) and the 
"for demo purposes" auto-fill language suggests dev/test scaffolding is reachable from the 
live call flow rather than being gated off.

**Bug**: Agent doesn't restate existing appointment details when informing a caller one is already booked
**Severity**: **Low-Medium**
**Call**: transcript-01.txt (CAfd393472b05fe634a7d014e44a9867c4) at 01:21
**Details**: When the agent detects an existing booking mid-call, it says only "It looks 
like you already have a routine check-up appointment booked. If you'd like, I can help you 
reschedule or cancel that appointment" -- no date, time, or provider given. This is 
inconsistent with how the same agent confirms a brand-new booking elsewhere in testing 
(full date/time/doctor read back, e.g. "Your appointment is set for Tuesday, July 21st at 
10am with Dr. ..."). A real patient checking on an existing appointment would have to ask 
a follow-up question just to hear when it actually is.

