# Member savings balance

`corelink.member.savings_balance` v0.1.0 — **draft**

Opens Member Search, enters the member ID, searches, and reads the Share Savings (S01) available balance from the Member Detail screen as savings_balance.

- App: `corelink-teller` versions `>=4.2,<5` (legacy_web)
- Side effects: **read**
- Recorded: run `discovery-20261021T185517-4716e7` on tenant `acme` v4.2.1 with `claude-sonnet-5-5`; human steps: 0
- Content hash: `sha256:51c86640698ba879a705de1ce8680b76`

## Inputs

| name | type | sensitivity | required | constraint | description |
|---|---|---|---|---|---|
| member_id | string | pii | True | ^\d+$ |  |

## Outputs

| name | type | sensitivity | from step | description |
|---|---|---|---|---|
| savings_balance | decimal | financial | s4 | Available balance of Share Savings (S01) |

## Steps

s1. **Open Member Search** (agent)  
   click [nav] role {'role': 'link', 'name': 'Member Search', 'exact': True} (+3 fallbacks: text, css, xpath)  
   expect: [main] at `/mbr/find`  
   verified by: `route` (frame main)
s2. **Enter member ID** (agent)  
   fill `{{member_id}}` into [main] label {'anchor': 'Member ID:', 'relation': 'next_control', 'role': 'textbox', 'cell_offset': 1} (+2 fallbacks: css, xpath)  
   verified by: `readback`
s3. **Search** (agent)  
   click [main] role {'role': 'button', 'name': 'Search', 'exact': True} (+4 fallbacks: label, text, css, xpath)  
   expect: [main] at `/mbr/result?q1={{member_id}}`  
   verified by: `route` (frame main)
s4. **Read savings balance** (agent)  
   extract → `savings_balance` (decimal) from [main] label {'anchor': 'Share Savings', 'relation': 'row_cell', 'cell_offset': 1} (+2 fallbacks: css, xpath)  
   require: value at 'Account #:' shows {{member_id}}  
   verified by: `value_parse`

## Success checkpoint

([main] at `/mbr/result?q1={{member_id}}`) ALL (text /Member\ Detail/ visible)

## Business invariants

- `shows_member_id`: value at 'Account #:' shows {{member_id}}

Record identity: `member_id` displayed on screen

## Business outcomes the caller may receive

- (capability-specific: none; product-level detectors from the app profile also apply: NOT_FOUND, ACCESS_DENIED, VALIDATION_ERROR)
