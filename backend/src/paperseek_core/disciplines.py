
from dataclasses import asdict, dataclass
import re
from typing import Iterable


@dataclass(frozen=True)
class DisciplineField:
    id: str
    label: str
    domain: str
    wos_categories: tuple[str, ...]

    @property
    def openalex_field_id(self) -> int:
        return int(self.id)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["openalex_field_id"] = self.openalex_field_id
        data["openalex_filter"] = f"primary_topic.field.id:{self.id}"
        data["wos_categories"] = list(self.wos_categories)
        return data


OPENALEX_FIELDS: tuple[DisciplineField, ...] = (
    DisciplineField("11", "Agricultural and Biological Sciences", "Life Sciences", ("Agricultural Economics & Policy", "Agricultural Engineering", "Agriculture, Dairy & Animal Science", "Agriculture, Multidisciplinary", "Agronomy", "Biodiversity Conservation", "Biology", "Ecology", "Entomology", "Fisheries", "Food Science & Technology", "Forestry", "Horticulture", "Marine & Freshwater Biology", "Plant Sciences", "Soil Science", "Zoology")),
    DisciplineField("12", "Arts and Humanities", "Social Sciences", ("Architecture", "Art", "Asian Studies", "Classics", "Cultural Studies", "Dance", "Film, Radio, Television", "Folklore", "History", "History & Philosophy of Science", "Humanities, Multidisciplinary", "Language & Linguistics", "Literary Reviews", "Literary Theory & Criticism", "Literature", "Music", "Poetry", "Religion", "Theater")),
    DisciplineField("13", "Biochemistry, Genetics and Molecular Biology", "Life Sciences", ("Biochemical Research Methods", "Biochemistry & Molecular Biology", "Biotechnology & Applied Microbiology", "Cell Biology", "Developmental Biology", "Genetics & Heredity", "Mathematical & Computational Biology", "Physiology", "Reproductive Biology")),
    DisciplineField("14", "Business, Management and Accounting", "Social Sciences", ("Business", "Business, Finance", "Hospitality, Leisure, Sport & Tourism", "Industrial Relations & Labor", "Management", "Operations Research & Management Science")),
    DisciplineField("15", "Chemical Engineering", "Physical Sciences", ("Chemistry, Applied", "Engineering, Chemical", "Energy & Fuels", "Polymer Science", "Thermodynamics")),
    DisciplineField("16", "Chemistry", "Physical Sciences", ("Chemistry, Analytical", "Chemistry, Applied", "Chemistry, Inorganic & Nuclear", "Chemistry, Medicinal", "Chemistry, Multidisciplinary", "Chemistry, Organic", "Chemistry, Physical", "Crystallography", "Electrochemistry", "Spectroscopy")),
    DisciplineField("17", "Computer Science", "Physical Sciences", ("Computer Science, Artificial Intelligence", "Computer Science, Cybernetics", "Computer Science, Hardware & Architecture", "Computer Science, Information Systems", "Computer Science, Interdisciplinary Applications", "Computer Science, Software Engineering", "Computer Science, Theory & Methods", "Information Science & Library Science", "Robotics", "Telecommunications")),
    DisciplineField("18", "Decision Sciences", "Social Sciences", ("Management", "Operations Research & Management Science", "Social Sciences, Mathematical Methods", "Statistics & Probability")),
    DisciplineField("19", "Earth and Planetary Sciences", "Physical Sciences", ("Astronomy & Astrophysics", "Geochemistry & Geophysics", "Geography, Physical", "Geology", "Geosciences, Multidisciplinary", "Meteorology & Atmospheric Sciences", "Mineralogy", "Oceanography", "Paleontology", "Remote Sensing")),
    DisciplineField("20", "Economics, Econometrics and Finance", "Social Sciences", ("Agricultural Economics & Policy", "Business, Finance", "Development Studies", "Economics", "Industrial Relations & Labor", "Regional & Urban Planning")),
    DisciplineField("21", "Energy", "Physical Sciences", ("Energy & Fuels", "Engineering, Petroleum", "Environmental Sciences", "Green & Sustainable Science & Technology", "Nuclear Science & Technology")),
    DisciplineField("22", "Engineering", "Physical Sciences", ("Automation & Control Systems", "Construction & Building Technology", "Engineering, Aerospace", "Engineering, Biomedical", "Engineering, Civil", "Engineering, Electrical & Electronic", "Engineering, Environmental", "Engineering, Geological", "Engineering, Industrial", "Engineering, Manufacturing", "Engineering, Marine", "Engineering, Mechanical", "Engineering, Multidisciplinary", "Engineering, Ocean", "Engineering, Petroleum", "Instruments & Instrumentation", "Mechanics", "Robotics", "Transportation Science & Technology")),
    DisciplineField("23", "Environmental Science", "Physical Sciences", ("Biodiversity Conservation", "Ecology", "Environmental Sciences", "Environmental Studies", "Green & Sustainable Science & Technology", "Limnology", "Remote Sensing", "Water Resources")),
    DisciplineField("24", "Immunology and Microbiology", "Life Sciences", ("Biotechnology & Applied Microbiology", "Immunology", "Infectious Diseases", "Microbiology", "Mycology", "Parasitology", "Virology")),
    DisciplineField("25", "Materials Science", "Physical Sciences", ("Materials Science, Biomaterials", "Materials Science, Ceramics", "Materials Science, Characterization & Testing", "Materials Science, Coatings & Films", "Materials Science, Composites", "Materials Science, Multidisciplinary", "Materials Science, Paper & Wood", "Materials Science, Textiles", "Metallurgy & Metallurgical Engineering", "Nanoscience & Nanotechnology", "Polymer Science")),
    DisciplineField("26", "Mathematics", "Physical Sciences", ("Logic", "Mathematical & Computational Biology", "Mathematics", "Mathematics, Applied", "Mathematics, Interdisciplinary Applications", "Statistics & Probability")),
    DisciplineField("27", "Medicine", "Health Sciences", ("Allergy", "Anatomy & Morphology", "Andrology", "Anesthesiology", "Cardiac & Cardiovascular Systems", "Clinical Neurology", "Critical Care Medicine", "Dermatology", "Emergency Medicine", "Endocrinology & Metabolism", "Gastroenterology & Hepatology", "Geriatrics & Gerontology", "Hematology", "Infectious Diseases", "Medicine, General & Internal", "Medicine, Research & Experimental", "Oncology", "Ophthalmology", "Orthopedics", "Pathology", "Pediatrics", "Psychiatry", "Public, Environmental & Occupational Health", "Radiology, Nuclear Medicine & Medical Imaging", "Respiratory System", "Surgery", "Urology & Nephrology")),
    DisciplineField("28", "Neuroscience", "Life Sciences", ("Behavioral Sciences", "Clinical Neurology", "Neuroimaging", "Neurosciences", "Psychology, Biological")),
    DisciplineField("29", "Nursing", "Health Sciences", ("Health Care Sciences & Services", "Nursing", "Primary Health Care", "Public, Environmental & Occupational Health")),
    DisciplineField("30", "Pharmacology, Toxicology and Pharmaceutics", "Life Sciences", ("Chemistry, Medicinal", "Pharmacology & Pharmacy", "Toxicology")),
    DisciplineField("31", "Physics and Astronomy", "Physical Sciences", ("Acoustics", "Astronomy & Astrophysics", "Optics", "Physics, Applied", "Physics, Atomic, Molecular & Chemical", "Physics, Condensed Matter", "Physics, Fluids & Plasmas", "Physics, Mathematical", "Physics, Multidisciplinary", "Physics, Nuclear", "Physics, Particles & Fields", "Quantum Science & Technology")),
    DisciplineField("32", "Psychology", "Social Sciences", ("Behavioral Sciences", "Psychiatry", "Psychology", "Psychology, Applied", "Psychology, Biological", "Psychology, Clinical", "Psychology, Developmental", "Psychology, Educational", "Psychology, Experimental", "Psychology, Mathematical", "Psychology, Multidisciplinary", "Psychology, Psychoanalysis", "Psychology, Social")),
    DisciplineField("33", "Social Sciences", "Social Sciences", ("Anthropology", "Area Studies", "Communication", "Criminology & Penology", "Demography", "Education & Educational Research", "Ethics", "Ethnic Studies", "Family Studies", "Geography", "International Relations", "Law", "Linguistics", "Political Science", "Public Administration", "Regional & Urban Planning", "Social Issues", "Social Sciences, Interdisciplinary", "Social Work", "Sociology", "Urban Studies", "Women's Studies")),
    DisciplineField("34", "Veterinary", "Health Sciences", ("Veterinary Sciences", "Zoology")),
    DisciplineField("35", "Dentistry", "Health Sciences", ("Dentistry, Oral Surgery & Medicine",)),
    DisciplineField("36", "Health Professions", "Health Sciences", ("Health Care Sciences & Services", "Health Policy & Services", "Medical Informatics", "Public, Environmental & Occupational Health", "Rehabilitation", "Sport Sciences")),
)


_BY_ID = {field.id: field for field in OPENALEX_FIELDS}


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


WOS_CORE_CATEGORIES: tuple[str, ...] = (
    "Acoustics",
    "Agricultural Economics & Policy",
    "Agricultural Engineering",
    "Agriculture, Dairy & Animal Science",
    "Agriculture, Multidisciplinary",
    "Agronomy",
    "Allergy",
    "Anatomy & Morphology",
    "Andrology",
    "Anesthesiology",
    "Anthropology",
    "Archaeology",
    "Architecture",
    "Area Studies",
    "Art",
    "Asian Studies",
    "Astronomy & Astrophysics",
    "Audiology & Speech-Language Pathology",
    "Automation & Control Systems",
    "Behavioral Sciences",
    "Biochemical Research Methods",
    "Biochemistry & Molecular Biology",
    "Biodiversity Conservation",
    "Biology",
    "Biophysics",
    "Biotechnology & Applied Microbiology",
    "Business",
    "Business, Finance",
    "Cardiac & Cardiovascular Systems",
    "Cell Biology",
    "Cell & Tissue Engineering",
    "Chemistry, Analytical",
    "Chemistry, Applied",
    "Chemistry, Inorganic & Nuclear",
    "Chemistry, Medicinal",
    "Chemistry, Multidisciplinary",
    "Chemistry, Organic",
    "Chemistry, Physical",
    "Classics",
    "Clinical Neurology",
    "Communication",
    "Computer Science, Artificial Intelligence",
    "Computer Science, Cybernetics",
    "Computer Science, Hardware & Architecture",
    "Computer Science, Information Systems",
    "Computer Science, Interdisciplinary Applications",
    "Computer Science, Software Engineering",
    "Computer Science, Theory & Methods",
    "Construction & Building Technology",
    "Criminology & Penology",
    "Critical Care Medicine",
    "Crystallography",
    "Cultural Studies",
    "Dance",
    "Demography",
    "Dentistry, Oral Surgery & Medicine",
    "Dermatology",
    "Development Studies",
    "Developmental Biology",
    "Ecology",
    "Economics",
    "Education & Educational Research",
    "Education, Scientific Disciplines",
    "Education, Special",
    "Electrochemistry",
    "Emergency Medicine",
    "Endocrinology & Metabolism",
    "Energy & Fuels",
    "Engineering, Aerospace",
    "Engineering, Biomedical",
    "Engineering, Chemical",
    "Engineering, Civil",
    "Engineering, Electrical & Electronic",
    "Engineering, Environmental",
    "Engineering, Geological",
    "Engineering, Industrial",
    "Engineering, Manufacturing",
    "Engineering, Marine",
    "Engineering, Mechanical",
    "Engineering, Multidisciplinary",
    "Engineering, Ocean",
    "Engineering, Petroleum",
    "Entomology",
    "Environmental Sciences",
    "Environmental Studies",
    "Ergonomics",
    "Ethics",
    "Ethnic Studies",
    "Evolutionary Biology",
    "Family Studies",
    "Film, Radio, Television",
    "Fisheries",
    "Folklore",
    "Food Science & Technology",
    "Forestry",
    "Gastroenterology & Hepatology",
    "Genetics & Heredity",
    "Geochemistry & Geophysics",
    "Geography",
    "Geography, Physical",
    "Geology",
    "Geosciences, Multidisciplinary",
    "Geriatrics & Gerontology",
    "Gerontology",
    "Green & Sustainable Science & Technology",
    "Health Care Sciences & Services",
    "Health Policy & Services",
    "Hematology",
    "History",
    "History & Philosophy of Science",
    "History of Social Sciences",
    "Horticulture",
    "Hospitality, Leisure, Sport & Tourism",
    "Humanities, Multidisciplinary",
    "Imaging Science & Photographic Technology",
    "Immunology",
    "Industrial Relations & Labor",
    "Infectious Diseases",
    "Information Science & Library Science",
    "Instruments & Instrumentation",
    "Integrative & Complementary Medicine",
    "International Relations",
    "Language & Linguistics",
    "Law",
    "Limnology",
    "Linguistics",
    "Literary Reviews",
    "Literary Theory & Criticism",
    "Literature",
    "Literature, African, Australian, Canadian",
    "Literature, American",
    "Literature, British Isles",
    "Literature, German, Dutch, Scandinavian",
    "Literature, Romance",
    "Literature, Slavic",
    "Logic",
    "Management",
    "Marine & Freshwater Biology",
    "Materials Science, Biomaterials",
    "Materials Science, Ceramics",
    "Materials Science, Characterization & Testing",
    "Materials Science, Coatings & Films",
    "Materials Science, Composites",
    "Materials Science, Multidisciplinary",
    "Materials Science, Paper & Wood",
    "Materials Science, Textiles",
    "Mathematical & Computational Biology",
    "Mathematics",
    "Mathematics, Applied",
    "Mathematics, Interdisciplinary Applications",
    "Mechanics",
    "Medical Ethics",
    "Medical Informatics",
    "Medical Laboratory Technology",
    "Medicine, General & Internal",
    "Medicine, Legal",
    "Medicine, Research & Experimental",
    "Medieval & Renaissance Studies",
    "Metallurgy & Metallurgical Engineering",
    "Meteorology & Atmospheric Sciences",
    "Microbiology",
    "Microscopy",
    "Mineralogy",
    "Mining & Mineral Processing",
    "Multidisciplinary Sciences",
    "Music",
    "Mycology",
    "Nanoscience & Nanotechnology",
    "Neuroimaging",
    "Neurosciences",
    "Nuclear Science & Technology",
    "Nursing",
    "Nutrition & Dietetics",
    "Obstetrics & Gynecology",
    "Oceanography",
    "Oncology",
    "Operations Research & Management Science",
    "Ophthalmology",
    "Optics",
    "Ornithology",
    "Orthopedics",
    "Otorhinolaryngology",
    "Paleontology",
    "Parasitology",
    "Pathology",
    "Pediatrics",
    "Peripheral Vascular Disease",
    "Pharmacology & Pharmacy",
    "Philosophy",
    "Physics, Applied",
    "Physics, Atomic, Molecular & Chemical",
    "Physics, Condensed Matter",
    "Physics, Fluids & Plasmas",
    "Physics, Mathematical",
    "Physics, Multidisciplinary",
    "Physics, Nuclear",
    "Physics, Particles & Fields",
    "Physiology",
    "Plant Sciences",
    "Poetry",
    "Political Science",
    "Polymer Science",
    "Primary Health Care",
    "Psychiatry",
    "Psychology",
    "Psychology, Applied",
    "Psychology, Biological",
    "Psychology, Clinical",
    "Psychology, Developmental",
    "Psychology, Educational",
    "Psychology, Experimental",
    "Psychology, Mathematical",
    "Psychology, Multidisciplinary",
    "Psychology, Psychoanalysis",
    "Psychology, Social",
    "Public Administration",
    "Public, Environmental & Occupational Health",
    "Quantum Science & Technology",
    "Radiology, Nuclear Medicine & Medical Imaging",
    "Regional & Urban Planning",
    "Rehabilitation",
    "Religion",
    "Remote Sensing",
    "Reproductive Biology",
    "Respiratory System",
    "Rheumatology",
    "Robotics",
    "Social Issues",
    "Social Sciences, Biomedical",
    "Social Sciences, Interdisciplinary",
    "Social Sciences, Mathematical Methods",
    "Social Work",
    "Sociology",
    "Soil Science",
    "Spectroscopy",
    "Sport Sciences",
    "Statistics & Probability",
    "Substance Abuse",
    "Surgery",
    "Telecommunications",
    "Theater",
    "Thermodynamics",
    "Toxicology",
    "Transplantation",
    "Transportation",
    "Transportation Science & Technology",
    "Tropical Medicine",
    "Urban Studies",
    "Urology & Nephrology",
    "Veterinary Sciences",
    "Virology",
    "Water Resources",
    "Women's Studies",
    "Zoology",
)

WOS_CATEGORIES: tuple[str, ...] = _unique(
    tuple(WOS_CORE_CATEGORIES)
    + tuple(category for field in OPENALEX_FIELDS for category in field.wos_categories)
)


ARXIV_CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("cs.AI", "Artificial Intelligence", "Computer Science"),
    ("cs.CL", "Computation and Language", "Computer Science"),
    ("cs.CV", "Computer Vision and Pattern Recognition", "Computer Science"),
    ("cs.DB", "Databases", "Computer Science"),
    ("cs.DC", "Distributed, Parallel, and Cluster Computing", "Computer Science"),
    ("cs.HC", "Human-Computer Interaction", "Computer Science"),
    ("cs.IR", "Information Retrieval", "Computer Science"),
    ("cs.LG", "Machine Learning", "Computer Science"),
    ("cs.NE", "Neural and Evolutionary Computing", "Computer Science"),
    ("cs.RO", "Robotics", "Computer Science"),
    ("cs.SE", "Software Engineering", "Computer Science"),
    ("cs.SI", "Social and Information Networks", "Computer Science"),
    ("stat.ML", "Machine Learning", "Statistics"),
    ("stat.AP", "Applications", "Statistics"),
    ("math.OC", "Optimization and Control", "Mathematics"),
    ("math.ST", "Statistics Theory", "Mathematics"),
    ("q-bio.BM", "Biomolecules", "Quantitative Biology"),
    ("q-bio.GN", "Genomics", "Quantitative Biology"),
    ("q-fin.EC", "Economics", "Quantitative Finance"),
    ("econ.GN", "General Economics", "Economics"),
    ("eess.SP", "Signal Processing", "Electrical Engineering and Systems Science"),
    ("physics.soc-ph", "Physics and Society", "Physics"),
)


SEMANTIC_SCHOLAR_FIELDS: tuple[str, ...] = (
    "Computer Science",
    "Medicine",
    "Chemistry",
    "Biology",
    "Materials Science",
    "Physics",
    "Geology",
    "Psychology",
    "Art",
    "History",
    "Geography",
    "Sociology",
    "Business",
    "Political Science",
    "Economics",
    "Philosophy",
    "Mathematics",
    "Engineering",
    "Environmental Science",
    "Agricultural and Food Sciences",
    "Education",
    "Law",
    "Linguistics",
)


def _alias_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://openalex\.org/fields/", "", text)
    text = text.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", text)


_ALIASES: dict[str, str] = {}
for _field in OPENALEX_FIELDS:
    for _value in (_field.id, _field.label, f"fields/{_field.id}", f"https://openalex.org/fields/{_field.id}"):
        _ALIASES[_alias_key(_value)] = _field.id


def _iter_values(values: object) -> Iterable[object]:
    if values is None:
        return ()
    if isinstance(values, str):
        if _alias_key(values) in _ALIASES:
            return (values,)
        parsed = []
        for part in re.split(r"[|;\n]+", values):
            part = part.strip()
            if not part:
                continue
            if _alias_key(part) in _ALIASES:
                parsed.append(part)
                continue
            comma_parts = [item.strip() for item in part.split(",") if item.strip()]
            if len(comma_parts) > 1 and all(_alias_key(item) in _ALIASES for item in comma_parts):
                parsed.extend(comma_parts)
            else:
                parsed.append(part)
        return parsed
    if isinstance(values, Iterable):
        return values
    return (values,)


def normalize_discipline_ids(values: object) -> tuple[str, ...]:
    ids: list[str] = []
    for value in _iter_values(values):
        key = _alias_key(value)
        discipline_id = _ALIASES.get(key)
        if not discipline_id:
            continue
        if discipline_id not in ids:
            ids.append(discipline_id)
    return tuple(ids)


def list_discipline_fields() -> list[dict[str, object]]:
    return [field.to_dict() for field in OPENALEX_FIELDS]


def list_source_filter_options(source: str) -> list[dict[str, object]]:
    source = (source or "openalex").strip().lower()
    if source == "openalex":
        return list_discipline_fields()
    if source == "wos":
        return [
            {"id": category, "label": category, "domain": "Web of Science Category"}
            for category in WOS_CATEGORIES
        ]
    if source == "arxiv":
        return [
            {"id": category, "label": label, "domain": group}
            for category, label, group in ARXIV_CATEGORIES
        ]
    return []


def source_filter_mode(source: str) -> str:
    source = (source or "openalex").strip().lower()
    if source in {"openalex", "wos", "arxiv"}:
        return "native"
    return "text"


def source_filter_label(source: str) -> str:
    source = (source or "openalex").strip().lower()
    labels = {
        "openalex": "OpenAlex Field",
        "wos": "Web of Science Category",
        "arxiv": "arXiv Category",
        "semanticscholar": "Field/context hint",
        "pubmed": "Biomedical field/context hint",
        "googlescholar": "Google Scholar field/context hint",
        "paperhub": "Computer science field/context hint",
        "crossref": "Bibliographic field/context hint",
        "wanfang": "Field/context hint",
        "nssd": "Social sciences field/context hint",
        "yiigle": "Medical field/context hint",
    }
    return labels.get(source, "Field/context hint")


def _normalize_by_choices(values: object, choices: Iterable[str]) -> tuple[str, ...]:
    by_key = {_alias_key(choice): choice for choice in choices}
    normalized: list[str] = []
    for value in _iter_values(values):
        match = by_key.get(_alias_key(value))
        if match and match not in normalized:
            normalized.append(match)
    return tuple(normalized)


def normalize_wos_categories(values: object) -> tuple[str, ...]:
    categories = _normalize_by_choices(values, WOS_CATEGORIES)
    if categories:
        return categories
    mapped = []
    for field in get_discipline_fields(values):
        for category in field.wos_categories:
            if category not in mapped:
                mapped.append(category)
    return tuple(mapped)


def normalize_arxiv_categories(values: object) -> tuple[str, ...]:
    return _normalize_by_choices(values, (category for category, _, _ in ARXIV_CATEGORIES))


def normalize_source_filter_values(source: str, values: object) -> tuple[str, ...]:
    source = (source or "openalex").strip().lower()
    if source == "openalex":
        return normalize_discipline_ids(values)
    if source == "wos":
        return normalize_wos_categories(values)
    if source == "arxiv":
        return normalize_arxiv_categories(values)
    return ()


def get_discipline_fields(values: object) -> tuple[DisciplineField, ...]:
    return tuple(_BY_ID[field_id] for field_id in normalize_discipline_ids(values))


def discipline_labels(values: object) -> tuple[str, ...]:
    return tuple(field.label for field in get_discipline_fields(values))


def discipline_summary(values: object) -> str:
    return ", ".join(discipline_labels(values))


def openalex_field_ids(values: object) -> tuple[str, ...]:
    return normalize_discipline_ids(values)


def openalex_field_filter(values: object) -> str:
    ids = openalex_field_ids(values)
    if not ids:
        return ""
    return "primary_topic.field.id:" + "|".join(ids)


def wos_categories(values: object) -> tuple[str, ...]:
    return normalize_wos_categories(values)


def wos_category_clause(values: object, max_categories: int = 24) -> str:
    categories = wos_categories(values)[:max(1, int(max_categories or 24))]
    if not categories:
        return ""
    return "WC=(" + " OR ".join(categories) + ")"


def strip_wos_category_filter(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return ""
    query = re.sub(r"\s+(AND|OR)\s+WC\s*=\s*\([^)]*\)", "", query, flags=re.IGNORECASE)
    query = re.sub(r"WC\s*=\s*\([^)]*\)\s+(AND|OR)\s+", "", query, flags=re.IGNORECASE)
    query = re.sub(r"^WC\s*=\s*\([^)]*\)$", "", query, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", query).strip()


def _outer_parentheses_wrap(query: str) -> bool:
    query = query.strip()
    if not (query.startswith("(") and query.endswith(")")):
        return False
    depth = 0
    for index, char in enumerate(query):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
            if depth == 0 and index != len(query) - 1:
                return False
    return depth == 0


def _query_with_outer_group_for_filter(query: str) -> str:
    query = query.strip()
    if not query or _outer_parentheses_wrap(query):
        return query
    if re.search(r"\bOR\b", query, flags=re.IGNORECASE):
        return f"({query})"
    return query


def apply_wos_discipline_filter(query: str, values: object) -> str:
    del values
    return strip_wos_category_filter(query)


def arxiv_category_clause(values: object) -> str:
    categories = normalize_arxiv_categories(values)
    if not categories:
        return ""
    return "(" + " OR ".join(f"cat:{category}" for category in categories) + ")"


def apply_arxiv_category_filter(query: str, values: object) -> str:
    query = (query or "").strip()
    clause = arxiv_category_clause(values)
    if not clause:
        return query
    if re.search(r"\bcat\s*:", query, flags=re.IGNORECASE):
        return query
    filtered_query = _query_with_outer_group_for_filter(query)
    return f"{filtered_query} AND {clause}" if filtered_query else clause


def discipline_prompt_context(values: object, source: str, text_hint: str = "") -> str:
    source = (source or "").strip().lower()
    native_values = normalize_source_filter_values(source, values)
    hint = str(text_hint or "").strip()
    if source == "openalex":
        if not native_values:
            return ""
        labels = ", ".join(discipline_labels(native_values))
        return (
            f"\nDiscipline limit: {labels}.\n"
            f"The source request will apply OpenAlex filter={openalex_field_filter(native_values)}. "
            "Keep the search terms consistent with the selected disciplines, but do not output API parameters."
        )
    if source == "wos":
        if not native_values:
            return ""
        labels = ", ".join(native_values)
        return (
            f"\nWeb of Science Category context: {labels}.\n"
            "The Web of Science Starter API used by PaperSeek does not accept WC= category filters. "
            "Use these categories only to choose better TS/TI/SO terms, and do not output WC=."
        )
    if source == "arxiv":
        if not native_values:
            return ""
        labels = ", ".join(native_values)
        return (
            f"\narXiv category limit: {labels}.\n"
            f"The final source request will apply {arxiv_category_clause(native_values)}. "
            "Keep the search terms consistent with those categories, but do not add another cat: clause."
        )
    if hint and source in {"wanfang", "nssd", "yiigle"}:
        return f"\n研究领域提示：{hint}。\n该数据库在 PaperSeek 中没有可靠的学科过滤，只用这条提示选择更合适的中文检索词。"
    if hint:
        return (
            f"\nResearch field/context hint: {hint}.\n"
            "This source has no reliable native discipline filter in PaperSeek, so use this hint only to choose better search terms."
        )
    return ""


def discipline_source_note(values: object, source: str) -> str:
    source = (source or "").strip().lower()
    native_values = normalize_source_filter_values(source, values)
    if not native_values:
        return ""
    if source == "openalex":
        labels = ", ".join(discipline_labels(native_values))
        return f"discipline={labels}; filter={openalex_field_filter(native_values)}"
    if source == "wos":
        return f"category_context={', '.join(native_values)}; WC unsupported by WoS Starter API"
    if source == "arxiv":
        return f"category={', '.join(native_values)}; {arxiv_category_clause(native_values)}"
    return ""
