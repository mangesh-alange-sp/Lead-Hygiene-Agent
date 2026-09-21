# Reference Taxonomy

Alias tables only. `pipeline/taxonomy.py` reads each `##` section as `{alias: canonical}`.

## Title

| Alias | Canonical |
|---|---|
| svp | Senior Vice President |
| vp | Vice President |
| ciso | Chief Information Security Officer |
| cio | Chief Information Officer |
| cto | Chief Technology Officer |
| ceo | CEO |
| chief executive officer | CEO |
| chief exec | CEO |
| cfo | CFO |
| chief financial officer | CFO |
| chief technology officer | Chief Technology Officer |
| chief tech officer | Chief Technology Officer |
| chief information security officer | Chief Information Security Officer |
| chief info security officer | Chief Information Security Officer |
| coo | COO |
| chief operating officer | COO |
| ae | Account Executive |
| sdr | Sales Development Representative |
| bdr | Business Development Representative |
| sae | Senior Account Executive |
| vp sales | VP of Sales |
| vice president of sales | VP of Sales |
| vp of sales | VP of Sales |
| vp marketing | VP of Marketing |
| vice president of marketing | VP of Marketing |
| vp engineering | VP of Engineering |
| vice president of engineering | VP of Engineering |
| vp it | VP of IT |
| vice president of it | VP of IT |
| director it | Director of IT |
| director of it | Director of IT |
| dir of it | Director of IT |
| director security | Director of Security |
| director of security | Director of Security |
| director sales | Director of Sales |
| director of sales | Director of Sales |
| senior manager | Senior Manager |
| sr manager | Senior Manager |
| senior mgr | Senior Manager |
| info sec manager | Information Security Manager |
| infosec manager | Information Security Manager |
| sys admin | Systems Administrator |
| system administrator | Systems Administrator |
| sysadmin | Systems Administrator |
| software dev | Software Developer |
| developer | Software Developer |
| software developer | Software Developer |
| software engineer | Software Engineer |
| swe | Software Engineer |
| it manager | IT Manager |
| manager of it | IT Manager |
| security analyst | Security Analyst |
| infosec analyst | Security Analyst |

## Company

| Alias | Canonical |
|---|---|
| pg | Procter & Gamble |
| p&g | Procter & Gamble |
| p and g | Procter & Gamble |
| procter and gamble | Procter & Gamble |
| procter & gamble | Procter & Gamble |
| amgen | Amgen Inc. |
| amgen inc | Amgen Inc. |
| amgen incorporated | Amgen Inc. |
| sailpoint | SailPoint Technologies |
| sailpoint tech | SailPoint Technologies |
| sailpoint technologies | SailPoint Technologies |
| sailpoint technologies inc | SailPoint Technologies |
| walmart | Walmart |
| walmart inc | Walmart |
| starbucks | Starbucks |
| starbucks coffee | Starbucks |
| mcdonalds | McDonald's |
| mcdonalds corporation | McDonald's |
| ms&ad | MS&AD Insurance Group |
| ms&ad insurance group | MS&AD Insurance Group |
| bcbs | BCBS Association |
| bcbs association | BCBS Association |
| flex | Flex |
| gnc | GNC |
| cvs | CVS Health |
| cvs health | CVS Health |
| autozone | AutoZone |
| wawa | Wawa |
| wawa inc | Wawa |
| acme inc | Acme Inc. |
| example llc | Example LLC |

## Invalid

| Alias | Canonical |
|---|---|
| test company | INVALID |
| n/a | INVALID |
| unknown | INVALID |
| missing co | INVALID |

## Industry

Nicknames only. Official Salesforce labels are matched from `data/salesforce_picklists.json`.

| Alias | Canonical |
|---|---|
| software | Technology |
| computer software | Technology |
| tech | Technology |
| saas | Technology |
| it | Technology |
| software and technology | Technology |
| business services | Professional Services |
| consulting | Professional Services |
| advisory | Professional Services |
| finance | Financial Services |
| banking | Financial Services |
| fintech | Financial Services |
| travel | Hospitality |
| hotels | Hospitality |
| tourism | Hospitality |
| real estate & leasing | Real Estate & Construction |
| real estate | Real Estate & Construction |
| property | Real Estate & Construction |
| oil and gas | Energy & Utilities |
| power | Energy & Utilities |
| energy utilities & waste | Energy & Utilities |
| primary/secondary education | Education |
| edu | Education |
| university | Education |
| higher education | Education |
| consumer goods | Retail |
| ecommerce | Retail |
| e-commerce | Retail |
| electrical/electronic manufacturing | Manufacturing |
| packaging and containers | Manufacturing |
| industrial | Manufacturing |
| production | Manufacturing |
| automotive | Manufacturing |
| auto | Manufacturing |
| medical | Healthcare |
| pharma | Healthcare |
| life sciences | Healthcare |
| healthcare and medical | Healthcare |
| govt | Government |
| public sector | Government |
| federal | Government |
| telecom | Telecommunications |
| aerospace | Aerospace & Defense |
| defense | Aerospace & Defense |
| aerospace and defense | Aerospace & Defense |
| logistics | Transportation & Logistics |
| supply chain | Transportation & Logistics |
| law firm | Legal |
| law | Legal |
| nonprofit | Non-Profit |
| ngo | Non-Profit |
| food | Food & Beverage |
| f&b | Food & Beverage |
| fmcg | Food & Beverage |
| publishing | Media & Entertainment |
| technology, information & media | Technology |
| technology information and media | Technology |
| community & nonprofit organizations | Non-Profit |
| community and nonprofit organizations | Non-Profit |
| retail & wholesale trade | Retail |
| retail and wholesale trade | Retail |
| oil, gas & mining | Energy |
| oil gas and mining | Energy |

## Country

| Alias | Canonical |
|---|---|
| us | United States |
| usa | United States |
| u.s. | United States |
| united states of america | United States |
| uk | United Kingdom |
| u.k. | United Kingdom |
| great britain | United Kingdom |
| england | United Kingdom |
| in | India |
| bharat | India |
| ca | Canada |
| au | Australia |
| aus | Australia |
| de | Germany |
| deutschland | Germany |
| fr | France |
| jp | Japan |
| sg | Singapore |
| nl | Netherlands |
| holland | Netherlands |
| ie | Ireland |
| eire | Ireland |

## State

Abbreviations only. Full names match the Salesforce state list. `ca` becomes California only when Country is United States.

| Alias | Canonical |
|---|---|
| tx | Texas |
| tex | Texas |
| ca | California |
| calif | California |
| ny | New York |
| n.y. | New York |
| wa | Washington |
| wash | Washington |
| ma | Massachusetts |
| mass | Massachusetts |
| on | Ontario |
| bc | British Columbia |

## Street

| Alias | Canonical |
|---|---|
| street | St. |
| st | St. |
| road | Rd. |
| rd | Rd. |
| avenue | Ave. |
| ave | Ave. |
| boulevard | Blvd. |
| blvd | Blvd. |
| drive | Dr. |
| dr | Dr. |
| highway | Hwy. |
| hwy | Hwy. |
| north | N |
| south | S |
| east | E |
| west | W |

## Lead Source

| Alias | Canonical |
|---|---|
| website form | Web |
| web form | Web |
| online form | Web |
| webinar | Event |
| conference | Event |
| trade show | Event |
| reseller | Partner |
| referral partner | Partner |
| app marketplace | Marketplace |
| cold call | Outbound |
| sales prospecting | Outbound |
| organic search | Inbound |
| website | Inbound |
| content download | Content |
| ebook | Content |
| whitepaper | Content |

## Status

Do not map statuses here. Legal values come from the Salesforce picklist. Hygiene never rewrites a sales stage.

## Consent

| Alias | Canonical |
|---|---|
| yes | true |
| true | true |
| opted in | true |
| subscribed | true |
| no | false |
| false | false |
| opted out | false |
| unsubscribed | false |
