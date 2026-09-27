"""Classification schemas for local news and community sentiment."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

NO_CONCERN = "no concern raised"


class ParagraphLabels(BaseModel):
    """Labels assigned to one paragraph by the typed classifier."""

    relevant: bool = Field(description="The text is about an energy project or infrastructure near a local community.")
    # Measured on real news paragraphs with the Modal classifier: "The text's attitude to the project" called plain
    # reports of plans supportive; this wording leaves them neutral and keeps clear objections and support.
    stance: Literal["against", "neutral", "supportive"] = Field(
        description="Does the text express or report opposition to the project, support for it, or neither?"
    )
    # Without a "none" option every paragraph had to name a concern, so a consent date came out as "fire safety".
    concern: Literal[
        "no concern raised", "fire safety", "noise", "visual impact", "traffic", "land use", "ecology", "other"
    ] = Field(
        description="Which worry or objection about the project does the text raise? "
        f"Choose '{NO_CONCERN}' if it raises none."
    )
    mentions_risk: bool = Field(description="The text mentions a risk for a battery storage project.")
