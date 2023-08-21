from typing import Any, Literal, Optional

import pydantic

from cachi2.core.models.validators import unique_sorted


class Property(pydantic.BaseModel):
    """A property inside an SBOM component."""

    name: str
    value: str


FOUND_BY_CACHI2_PROPERTY: Property = Property(name="cachi2:found_by", value="cachi2")


class Component(pydantic.BaseModel):
    """A software component such as a dependency or a package.

    Compliant to the CycloneDX specification:
    https://cyclonedx.org/docs/1.4/json/#components
    """

    name: str
    version: Optional[str]
    properties: list[Property] = []
    purl: Optional[str]  # optional while it is not implemented for all package managers
    type: Literal["library"] = "library"

    def key(self) -> str:
        """Uniquely identifies a package.

        Used mainly for sorting and deduplication.
        """
        if self.purl:
            return self.purl

        return f"{self.name}:{self.version}"

    @pydantic.validator("properties", always=True)
    def _add_found_by_property(cls, properties: list[Property]) -> list[Property]:
        if FOUND_BY_CACHI2_PROPERTY not in properties:
            properties.insert(0, FOUND_BY_CACHI2_PROPERTY)

        return properties

    @classmethod
    def from_package_dict(cls, package: dict[str, Any]) -> "Component":
        """Create a Component from a Cachi2 package dictionary.

        A Cachi2 package has extra fields which are unnecessary and can cause validation errors.
        """
        return Component(
            name=package.get("name", None),
            version=package.get("version", None),
            purl=package.get("purl", None),
        )


class Tool(pydantic.BaseModel):
    """A tool used to generate the SBOM content."""

    vendor: str
    name: str


class Metadata(pydantic.BaseModel):
    """Metadata field in a SBOM."""

    tools: list[Tool] = [Tool(vendor="red hat", name="cachi2")]


class Sbom(pydantic.BaseModel):
    """Software bill of materials in the CycloneDX format.

    See full specification at:
    https://cyclonedx.org/docs/1.4/json
    """

    bom_format: Literal["CycloneDX"] = pydantic.Field(alias="bomFormat", default="CycloneDX")
    components: list[Component] = []
    metadata: Metadata = Metadata()
    spec_version: str = pydantic.Field(alias="specVersion", default="1.4")
    version: int = 1

    @pydantic.validator("components")
    def _unique_components(cls, components: list[Component]) -> list[Component]:
        """Sort and de-duplicate components."""
        return unique_sorted(components, by=lambda component: component.key())