"""Catalog API, shaped like the source data:
- products: prod.json entries, by tool_id (CFProdCode-<n>) — name and url asked for
- categories / industries: by slug and name
- news, customer stories, alternatives, comparisons: type + url + title given explicitly
- use cases: product_usecases.json entries, by UUID
Changes are live at once; derived data (popularity, titles, use-case top products, categories) is kept in sync."""
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

from config import SOURCE_URL
from scripts import catalog, source

router = APIRouter(prefix="/api")
ToolId = Annotated[str, Path(pattern=r"^CFProdCode-\d+$", examples=["CFProdCode-17370"])]
UseCaseId = Annotated[str, Path(min_length=1, max_length=100, examples=["62eef0e3-6428-46cb-8149-c679c47d9ab6"])]
Slug = Annotated[str, Path(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", examples=["account-based-marketing"])]
PAGE_URL_EXAMPLE = "https://www.cuspera.com/products/acme-signals-x-99999/news"


def _pid(tool_id: str) -> int:
    return int(tool_id.rsplit("-", 1)[-1])


# ---- products ---------------------------------------------------------------------------
class ProductCreate(BaseModel):
    tool_id: str = Field(pattern=r"^CFProdCode-\d+$", examples=["CFProdCode-99999"])
    name: str = Field(min_length=1, max_length=200, examples=["Acme Signals"])
    url: str | None = Field(None, min_length=1, max_length=1000,
                            examples=["https://www.cuspera.com/products/acme-signals-x-99999"],
                            description="Its product page; with it the product is searchable")


class ProductUpdate(BaseModel):
    """Omitted fields are kept. url: null removes the page (hidden, record kept)."""
    name: str | None = Field(None, min_length=1, max_length=200, examples=["Acme Signals"])
    url: str | None = Field(None, min_length=1, max_length=1000)


@router.post("/products", status_code=201, tags=["products"])
def create_product(record: ProductCreate):
    """Create a product (its prod.json record). Add `url` to publish its page at once."""
    return catalog.create_product(_pid(record.tool_id), record.name, record.url)


@router.get("/products/{tool_id}", tags=["products"])
def get_product(tool_id: ToolId):
    """The record, its use cases, and whether it is searchable (has a product page)."""
    return catalog.get_product(_pid(tool_id))


@router.patch("/products/{tool_id}", tags=["products"])
def update_product(tool_id: ToolId, patch: ProductUpdate):
    """Change name / url / cat / cats on the record."""
    fields = patch.model_dump(exclude_unset=True)
    if "name" in fields:
        fields["soft"] = fields.pop("name")
    return catalog.update_product(_pid(tool_id), **fields)


@router.delete("/products/{tool_id}", status_code=204, tags=["products"])
def delete_product(tool_id: ToolId):
    """Removes the product with its use cases, pages and comparisons."""
    catalog.delete_product(_pid(tool_id))
    return Response(status_code=204)


# ---- categories and industries ---------------------------------------------------------------
class TopicCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", examples=["demand-generation"])
    name: str = Field(min_length=1, max_length=200, examples=["Demand Generation"])


class TopicUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200, examples=["Demand Generation"])


@router.post("/categories", status_code=201, tags=["categories"])
def create_category(body: TopicCreate):
    """Create a category page (`/categories/{slug}`)."""
    return catalog.create_topic("categories", body.slug, body.name)


@router.get("/categories/{slug}", tags=["categories"])
def get_category(slug: Slug):
    return catalog.get_topic("categories", slug)


@router.patch("/categories/{slug}", tags=["categories"])
def update_category(slug: Slug, body: TopicUpdate):
    """Rename a category."""
    return catalog.update_topic("categories", slug, body.name)


@router.delete("/categories/{slug}", status_code=204, tags=["categories"])
def delete_category(slug: Slug):
    catalog.delete_topic("categories", slug)
    return Response(status_code=204)


@router.post("/industries", status_code=201, tags=["industries"])
def create_industry(body: TopicCreate):
    """Create an industry page (`/industries/{slug}`)."""
    return catalog.create_topic("industries", body.slug, body.name)


@router.get("/industries/{slug}", tags=["industries"])
def get_industry(slug: Slug):
    return catalog.get_topic("industries", slug)


@router.patch("/industries/{slug}", tags=["industries"])
def update_industry(slug: Slug, body: TopicUpdate):
    """Rename an industry."""
    return catalog.update_topic("industries", slug, body.name)


@router.delete("/industries/{slug}", status_code=204, tags=["industries"])
def delete_industry(slug: Slug):
    catalog.delete_topic("industries", slug)
    return Response(status_code=204)


# ---- use cases -------------------------------------------------------------------------------
class UseCase(BaseModel):
    """A product_usecases.json entry."""
    id: str = Field(min_length=1, max_length=100, examples=["62eef0e3-6428-46cb-8149-c679c47d9ab6"])
    name: str = Field(min_length=1, max_length=300, examples=["Lead scoring"])


class UseCaseName(BaseModel):
    name: str = Field(min_length=1, max_length=300, examples=["Predictive lead scoring"])


@router.put("/products/{tool_id}/use-cases", tags=["use cases"])
def set_use_cases(tool_id: ToolId, use_cases: list[UseCase]):
    """Replace the product's use-case list (a product_usecases.json entry; [] removes all)."""
    return catalog.set_use_cases(_pid(tool_id), [u.model_dump() for u in use_cases])


@router.post("/products/{tool_id}/use-cases", status_code=201, tags=["use cases"])
def add_use_case(tool_id: ToolId, use_case: UseCase):
    """Append one use case to the product's list; returns the whole list."""
    return catalog.add_use_case(_pid(tool_id), use_case.model_dump())


@router.delete("/products/{tool_id}/use-cases/{use_case_id}", tags=["use cases"])
def remove_use_case(tool_id: ToolId, use_case_id: UseCaseId):
    """Remove one use case from this product's list; returns the list."""
    return catalog.remove_use_case(_pid(tool_id), use_case_id)


@router.patch("/use-cases/{use_case_id}", tags=["use cases"])
def rename_use_case(use_case_id: UseCaseId, body: UseCaseName):
    """Rename a use case for every product that has it."""
    return catalog.rename_use_case(use_case_id, body.name)


@router.delete("/use-cases/{use_case_id}", status_code=204, tags=["use cases"])
def delete_use_case(use_case_id: UseCaseId):
    """Remove a use case from every product."""
    catalog.delete_use_case(use_case_id)
    return Response(status_code=204)


# ---- website pages (news, customer stories, alternatives, comparisons) --------------------------
PAGE_TYPES = {"news": "news", "customer-story": "customer_story", "alternatives": "alternatives",
              "compare": "compare"}


class PageCreate(BaseModel):
    type: Literal["news", "customer-story", "alternatives", "compare"]
    url: str = Field(min_length=1, max_length=1000, examples=[PAGE_URL_EXAMPLE])
    title: str | None = Field(None, min_length=1, max_length=300, examples=["Acme Signals News"],
                              description="Default: '<product> News' when linked, else from the url")
    product_ids: list[int] = Field(default_factory=list, max_length=2,
                                   examples=[[99999], [99999, 66]],
                                   description="Products it belongs to: one for a product page, "
                                               "two for a comparison, none for a vendor page")
    popularity: int | None = Field(None, ge=0)


PageUrl = Annotated[str, Query(min_length=1, max_length=1000, examples=[PAGE_URL_EXAMPLE])]


@router.post("/pages", status_code=201, tags=["pages"])
def add_page(page: PageCreate):
    """Add a news, customer-story, alternatives or compare page, by type and url."""
    return catalog.add_page(PAGE_TYPES[page.type], page.url, page.title, page.product_ids,
                            page.popularity)


@router.get("/pages", tags=["pages"])
def get_page(url: PageUrl):
    """One website page, by URL."""
    return catalog.get_page(url)


@router.delete("/pages", status_code=204, tags=["pages"])
def remove_page(url: PageUrl):
    """Remove a website page by URL."""
    catalog.remove_page(url)
    return Response(status_code=204)


# ---- source file sync -------------------------------------------------------------------------
@router.post("/sync", tags=["sync"])
def sync_source(dry_run: bool = Query(False, description="Only report what would change"),
                rebuild: bool = Query(False, description="A fresh index from the file alone (slower: "
                                                         "every use case is embedded again)")):
    """Make the index match the catalog source file (SOURCE_URL: JSON Lines, see scripts/source.py).
    Only changed documents are written; the file replaces changes made through this API.
    An invalid file changes nothing (422, with the first errors)."""
    try:
        return source.rebuild(SOURCE_URL) if rebuild else source.sync(SOURCE_URL, dry_run=dry_run)
    except OSError as e:                               # file missing / URL unreachable
        raise HTTPException(502, f"Cannot read the source file {SOURCE_URL}: {e}")
