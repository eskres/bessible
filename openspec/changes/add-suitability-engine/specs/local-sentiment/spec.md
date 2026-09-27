## ADDED Requirements

### Requirement: Local coverage is researched for the confirmed site
The system SHALL search for recent local news and planning coverage about energy and electricity infrastructure (battery storage, solar, wind, substations, pylons, cable routes, grid connections) using the site's place name, its district council and its county council. Coverage found through the place SHALL be kept only for projects within about 10 km of it; coverage found through a council SHALL be kept for projects anywhere in that council's area. It SHALL keep only UK sources, or pages that name the area, and SHALL record each source's URL, title and publication date where available. Every quote SHALL appear verbatim in the fetched page text.

#### Scenario: Coverage found
- **WHEN** the site is confirmed in an area with local coverage of batteries, solar farms or substations
- **THEN** the stage returns the sources it found, each with a URL

#### Scenario: No coverage
- **WHEN** no relevant coverage exists for the area
- **THEN** the stage completes with no sources, an opposition index of "unknown", and an artifact stating that nothing was found

### Requirement: Each paragraph is labelled with a confidence
The system SHALL split each source into paragraphs and label every paragraph for relevance, voice (residents, campaigners or councillors; the developer or its consultants; a reporter or official stating facts), stance towards the project (against, neutral or supportive), main concern (no concern raised, fire safety, noise, visual impact, traffic, land use, ecology, heritage, consultation or process, or other) and whether it mentions a risk. Every label SHALL carry a confidence between 0 and 1. Paragraphs labelled not relevant, and the developer's own statements, SHALL be excluded from the index.

#### Scenario: Objection paragraph
- **WHEN** a paragraph reports residents objecting to a nearby battery over fire risk
- **THEN** it is labelled relevant, residents, against, fire safety, mentions risk, each with a confidence

#### Scenario: Plain report
- **WHEN** a paragraph only reports that an application was submitted
- **THEN** its concern is "no concern raised" and it adds nothing to the top concerns

#### Scenario: Developer statement
- **WHEN** a paragraph is the developer describing its own project
- **THEN** it is shown as evidence, marked as not counted, and does not change the index

### Requirement: Opposition index and top concerns
The system SHALL compute an opposition index from 0 (supportive) to 1 (opposed), weighting each relevant paragraph's stance by its confidence, and SHALL list the top three concerns by weighted count. The same labels SHALL always give the same index.

#### Scenario: Mixed coverage
- **WHEN** two relevant paragraphs are against with confidence 0.9 and one is supportive with confidence 0.6
- **THEN** the index is above 0.5 and the top concern is taken from the two against paragraphs

### Requirement: Every labelled paragraph is evidence
The system SHALL emit one artifact per relevant paragraph with its source URL, a short quote, its labels, the confidence and the model that labelled it, and one artifact for the index that states how many paragraphs it counted and how many developer statements it left out.

#### Scenario: Evidence trail
- **WHEN** the report cites the opposition index
- **THEN** the cited artifact links to the paragraphs that produced it

### Requirement: Demo runs are repeatable
The system SHALL cache research results per site so a repeated run for the same site uses the same sources without searching again, and SHALL mark cached artifacts as cached.

#### Scenario: Offline rerun
- **WHEN** a demo site is run a second time with no network for search
- **THEN** the stage completes with the same sources and labels
