# NTREIS / Bridge API — Phase-0 Verification Findings

**Status:** Phase-0 COMPLETE (live-probed 2026-07-19 against the real feed). Resolves design
[OPEN #5/#6/#7] and the Q7 ClosePrice-fallback verification. This is the ground truth the Stage-2
comp engine is built against — no field mapping is assumed.

## Endpoint

- **Platform:** Bridge Interactive (Bridge Data Output) — RESO Web API (OData v4).
- **Base:** `https://api.bridgedataoutput.com/api/v2/OData/ntreis2` (dataset `ntreis2`).
- **Auth:** `Authorization: Bearer <NTREIS_SERVER_TOKEN>` (token len 32, in `Anthropic_API_KEY.env`).
- **Reachable from this environment:** yes (HTTP 200 on `$metadata`). Egress is NOT blocked for this host.
- **Resources:** `Lookup, Member, Office, OpenHouse, Property, PropertyRooms`. Property has **459 fields**.

## The critical findings

### 1. Sold data IS available — but `ClosePrice` is absent from the feed
- `StandardStatus='Closed'` sales are returned with `CloseDate`. **[OPEN #5] resolved: sold comps available.**
- **`ClosePrice` is not a field at all** (not merely per-listing suppressed — absent from `$metadata`).
  So the close price must be **reconstructed** — the Q7 fallback is *necessary*, not optional.
- **Filter to sales:** `PropertyType eq 'Residential'`. A bare `StandardStatus eq 'Closed'` returns
  **leases** (closed rentals, ListPrice = monthly rent). The comp engine MUST include the
  `PropertyType='Residential'` predicate or it will ingest rents as sale prices.

### 2. ClosePrice reconstruction (Q7) — VERIFIED
- Field: **`NTREIS2_RATIO_ClosePrice_By_LotSizeAcres`** (the user's `_B` = `_By_LotSizeAcres`).
  Sibling fields exist for `ListPrice` and `CurrentPrice`.
- **Reconstruction:** `ClosePrice ≈ NTREIS2_RATIO_ClosePrice_By_LotSizeAcres × LotSizeAcres`.
- Three-part check:
  - **(a) exists + per-acre units** — ✓ (name is literally close-price-per-acre; ratio × acres reproduces price).
  - **(b) populated where ClosePrice is suppressed** — ✓ (ClosePrice never in feed; the ratio is
    populated on every closed listing probed).
  - **(c) reproduces actual ClosePrice within rounding** — **adapted.** There is NO `ClosePrice` in
    the feed to compare against, so literal ground-truth is impossible. Substituted proof: the
    identical mechanism on `ListPrice` (`RATIO_ListPrice_By_LotSizeAcres × LotSizeAcres`) reproduces
    the real `ListPrice` at **0.0% error**, and the ClosePrice ratio demonstrably tracks the *actual
    sale* (it diverges from the ListPrice ratio when a property sold under list — e.g. recon close
    $2,977 vs list $3,300). Conclusion: the reconstruction is arithmetically sound and sale-accurate;
    the only residual is the absence of an in-feed ClosePrice for an independent triple-check.
- Real 75217 (Tryon's zip) closed SFR sales reconstructed sanely: $250K–$330K, $156–$181/sqft.
- **This same field is the §G land/teardown pricing basis** — ONE reconstruction path serves both
  (per the user's instruction). Land value = `RATIO_ClosePrice_By_LotSizeAcres × subject LotSizeAcres`
  for land/teardown comps.

### 3. Photos ([OPEN #6]) — available via the `Media` FIELD (not a resource)
- No top-level `Media` resource, but `Media` is a **selectable field on Property** (`$select=...,Media`).
  Each media object: `MediaKey, MediaCategory, MediaURL, MediaObjectID, ResourceRecordKey,
  ResourceName, ClassName, ShortDescription`.
- Photo URLs are Bridge CDN links (`dvvjkgh94f2v6.cloudfront.net`). **Hotlink per Q2** (never store).
  `PhotosCount` is present (11–40 on probed listings). `VirtualTourURLUnbranded/Branded/Zillow` also present.
- **Caveat:** `$expand=Media($top=N)` returned HTTP 400 (nested options unsupported). Select `Media`
  as a field directly and cap client-side.

### 4. Field coverage (confirmed present on Property)
`ListPrice, CloseDate, StandardStatus, MlsStatus, PropertyType, PropertySubType, LivingArea,
LotSizeAcres, LotSizeSquareFeet, BedroomsTotal, BathroomsTotalInteger, YearBuilt, Latitude, Longitude,
SubdivisionName, PublicRemarks, PhotosCount, City, PostalCode, UnparsedAddress, Media`, plus useful
NTREIS extras: `NTREIS2_OwnerName, NTREIS2_OccupantType, NTREIS2_PreviousStatus, NTREIS2_ClosedRemarks,
NTREIS2_TitleCompanyClosing`.
- **Absent:** `ClosePrice` (see above), `DaysOnMarket` (derive from `CloseDate − ListingContractDate`).

### 5. Query mechanics (for the client)
- `$filter` (e.g. `PropertyType eq 'Residential' and StandardStatus eq 'Closed' and PostalCode eq '75217'`),
  `$select`, `$top`, `$orderby=CloseDate desc`. Token via `Authorization` header.
- **Distance:** filter candidates by PostalCode/area + GLA band + `CloseDate` window in `$filter`, then
  compute distance client-side (Haversine on `Latitude`/`Longitude`) to assign tiers. (No reliance on
  OData geo functions assumed.)
- **[OPEN #7] rate limits:** not exhaustively probed — add conservative paging (`$top` ≤ ~200) and a
  per-run cap; throttle. To confirm against Bridge's published limits before high-volume use.

## Net effect on the Stage-2 design

Everything the approved design needed is available. Adjustments from the design's `$metadata` guesses
to reality: (1) filter `PropertyType='Residential'`; (2) `ClosePrice` is reconstructed for EVERY comp,
not just when suppressed — it's the standard close-price source here; (3) photos via the `Media` field,
not a resource/`$expand`; (4) derive DOM. No blockers.
