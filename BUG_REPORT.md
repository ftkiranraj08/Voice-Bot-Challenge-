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

