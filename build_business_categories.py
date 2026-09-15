#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Génère business_categories.json à partir de la nomenclature officielle NAF rév. 2 de l'INSEE.

Sortie :
{
  "43.22B": {
    "category": "Climatisation / chauffage",
    "keywords": [...]
  },
  ...
}

La NAF rév. 2 contient 732 sous-classes.
Source officielle : https://xml.insee.fr/schema/naf-enum.xsd
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SOURCE_URL = "https://xml.insee.fr/schema/naf-enum.xsd"
EXPECTED_COUNT = 732

XS_NS = "http://www.w3.org/2001/XMLSchema"
DC_NS = "http://purl.org/dc/elements/1.1/"

# Overrides : nom commercial + synonymes réellement utiles pour les recherches.
# Tous les autres codes reçoivent automatiquement des variantes issues du libellé INSEE.
OVERRIDES = {
    # Alimentaire / boissons
    "10.13B": {
        "category": "Charcuterie",
        "keywords": ["charcuterie", "charcutier", "traiteur charcutier", "boucherie charcuterie"],
    },
    "10.71C": {
        "category": "Boulangerie",
        "keywords": ["boulangerie", "boulanger", "boulangerie pâtisserie", "artisan boulanger", "pain artisanal"],
    },
    "10.71D": {
        "category": "Pâtisserie",
        "keywords": ["pâtisserie", "pâtissier", "artisan pâtissier", "gâteaux", "chocolaterie pâtisserie"],
    },
    "11.02A": {
        "category": "Vins effervescents",
        "keywords": ["producteur de champagne", "vins effervescents", "crémant", "producteur de vin effervescent"],
    },
    "11.02B": {
        "category": "Vin / domaine viticole",
        "keywords": ["domaine viticole", "vigneron", "producteur de vin", "cave viticole", "vinification"],
    },
    "11.05Z": {
        "category": "Brasserie / bière",
        "keywords": ["brasserie artisanale", "brasseur", "fabricant de bière", "microbrasserie", "production de bière"],
    },

    # Construction / habitat
    "41.10A": {
        "category": "Promotion immobilière",
        "keywords": ["promoteur immobilier", "promotion immobilière", "programme immobilier", "constructeur promoteur"],
    },
    "41.20A": {
        "category": "Construction de maisons",
        "keywords": ["constructeur maison", "constructeur maison individuelle", "construction maison", "maison neuve", "constructeur immobilier"],
    },
    "41.20B": {
        "category": "Construction de bâtiments",
        "keywords": ["entreprise générale bâtiment", "construction bâtiment", "constructeur bâtiment", "entreprise de construction"],
    },
    "43.11Z": {
        "category": "Démolition",
        "keywords": ["entreprise démolition", "démolition", "déconstruction", "curage bâtiment"],
    },
    "43.12A": {
        "category": "Terrassement",
        "keywords": ["terrassier", "entreprise terrassement", "terrassement", "travaux préparatoires", "excavation"],
    },
    "43.21A": {
        "category": "Électricité",
        "keywords": ["électricien", "entreprise électricité", "installation électrique", "électricien bâtiment", "dépannage électrique"],
    },
    "43.22A": {
        "category": "Plomberie / gaz",
        "keywords": ["plombier", "plomberie", "installation gaz", "plombier chauffagiste", "dépannage plomberie", "sanitaire"],
    },
    "43.22B": {
        "category": "Climatisation / chauffage",
        "keywords": [
            "climatisation",
            "installateur climatisation",
            "chauffagiste",
            "pompe à chaleur",
            "installation pompe à chaleur",
            "entretien climatisation",
            "climaticien",
            "chauffage",
            "installation chauffage",
            "chaudière",
            "ventilation",
            "CVC",
        ],
    },
    "43.29A": {
        "category": "Isolation",
        "keywords": ["isolation", "entreprise isolation", "isolation thermique", "isolation extérieure", "isolation combles", "ITE"],
    },
    "43.31Z": {
        "category": "Plâtrerie",
        "keywords": ["plaquiste", "plâtrier", "plâtrerie", "pose placo", "cloisons sèches"],
    },
    "43.32A": {
        "category": "Menuiserie",
        "keywords": [
            "menuisier",
            "menuiserie",
            "fenêtres PVC",
            "fenêtres aluminium",
            "portes",
            "volets roulants",
            "installateur fenêtres",
            "pose fenêtres",
            "menuiserie bois",
            "menuiserie PVC",
        ],
    },
    "43.32B": {
        "category": "Serrurerie / menuiserie métallique",
        "keywords": ["serrurier", "serrurerie", "menuiserie métallique", "porte métallique", "métallerie", "dépannage serrure"],
    },
    "43.32C": {
        "category": "Agencement de magasins",
        "keywords": ["agencement magasin", "agenceur", "aménagement commerce", "agencement boutique", "mobilier magasin"],
    },
    "43.33Z": {
        "category": "Carrelage / sols",
        "keywords": ["carreleur", "pose carrelage", "revêtement sol", "parqueteur", "solier", "revêtement mural"],
    },
    "43.34Z": {
        "category": "Peinture / vitrerie",
        "keywords": ["peintre bâtiment", "entreprise peinture", "vitrier", "vitrerie", "peinture intérieure", "peinture extérieure"],
    },
    "43.39Z": {
        "category": "Rénovation / finition",
        "keywords": ["entreprise rénovation", "rénovation intérieure", "travaux de finition", "rénovation bâtiment", "artisan rénovation"],
    },
    "43.91A": {
        "category": "Charpente",
        "keywords": ["charpentier", "charpente", "charpente bois", "entreprise charpente"],
    },
    "43.91B": {
        "category": "Couverture / toiture",
        "keywords": ["couvreur", "entreprise couverture", "toiture", "réfection toiture", "couvreur zingueur", "zinguerie"],
    },
    "43.99A": {
        "category": "Étanchéité",
        "keywords": ["étancheur", "étanchéité", "entreprise étanchéité", "étanchéité toiture", "étanchéité terrasse"],
    },
    "43.99B": {
        "category": "Structures métalliques",
        "keywords": ["montage structure métallique", "charpente métallique", "constructeur métallique", "métallerie industrielle"],
    },
    "43.99C": {
        "category": "Maçonnerie",
        "keywords": ["maçon", "maçonnerie", "entreprise maçonnerie", "gros œuvre", "maçonnerie générale"],
    },

    # Automobile
    "45.11Z": {
        "category": "Concession automobile",
        "keywords": ["concession automobile", "concessionnaire", "garage automobile vente", "vendeur voiture", "mandataire auto"],
    },
    "45.20A": {
        "category": "Garage automobile",
        "keywords": ["garage automobile", "garagiste", "mécanicien automobile", "réparation automobile", "centre auto", "entretien voiture"],
    },
    "45.32Z": {
        "category": "Pièces automobiles",
        "keywords": ["magasin pièces auto", "pièces automobiles", "accessoires auto", "équipement automobile"],
    },
    "45.40Z": {
        "category": "Moto / garage moto",
        "keywords": ["garage moto", "concession moto", "réparation moto", "magasin moto", "motocycle"],
    },

    # Commerces
    "47.11B": {
        "category": "Épicerie / alimentation",
        "keywords": ["épicerie", "alimentation générale", "épicerie de quartier", "commerce alimentaire"],
    },
    "47.11C": {
        "category": "Supérette",
        "keywords": ["supérette", "mini market", "commerce de proximité", "alimentation"],
    },
    "47.21Z": {
        "category": "Primeur",
        "keywords": ["primeur", "fruits et légumes", "magasin fruits légumes", "marchand de légumes"],
    },
    "47.22Z": {
        "category": "Boucherie",
        "keywords": ["boucherie", "boucher", "boucherie charcuterie", "viande"],
    },
    "47.23Z": {
        "category": "Poissonnerie",
        "keywords": ["poissonnerie", "poissonnier", "fruits de mer", "poissons"],
    },
    "47.24Z": {
        "category": "Boulangerie / pâtisserie",
        "keywords": ["boulangerie", "pâtisserie", "boulangerie pâtisserie", "artisan boulanger", "artisan pâtissier"],
    },
    "47.25Z": {
        "category": "Caviste",
        "keywords": ["caviste", "cave à vin", "magasin de vin", "vente de vins", "cave vins et spiritueux"],
    },
    "47.30Z": {
        "category": "Station-service",
        "keywords": ["station service", "station essence", "carburant", "station carburant"],
    },
    "47.52A": {
        "category": "Quincaillerie / bricolage",
        "keywords": ["quincaillerie", "magasin bricolage", "peinture bricolage", "outillage"],
    },
    "47.59A": {
        "category": "Magasin de meubles",
        "keywords": ["magasin meubles", "meubles", "ameublement", "mobilier", "showroom meubles"],
    },
    "47.64Z": {
        "category": "Magasin de sport",
        "keywords": ["magasin de sport", "articles de sport", "équipement sportif", "boutique sport"],
    },
    "47.71Z": {
        "category": "Magasin de vêtements",
        "keywords": ["magasin vêtements", "boutique vêtements", "prêt-à-porter", "boutique mode", "habillement"],
    },
    "47.72A": {
        "category": "Magasin de chaussures",
        "keywords": ["magasin chaussures", "chaussures", "boutique chaussures", "chausseur"],
    },
    "47.73Z": {
        "category": "Pharmacie",
        "keywords": ["pharmacie", "officine", "pharmacien"],
    },
    "47.75Z": {
        "category": "Parfumerie / beauté",
        "keywords": ["parfumerie", "magasin beauté", "cosmétiques", "boutique beauté"],
    },
    "47.76Z": {
        "category": "Fleuriste / animalerie / jardinerie",
        "keywords": ["fleuriste", "animalerie", "jardinerie", "fleurs", "plantes", "magasin animaux"],
    },
    "47.77Z": {
        "category": "Bijouterie / horlogerie",
        "keywords": ["bijouterie", "bijoutier", "joaillerie", "horlogerie", "horloger"],
    },
    "47.78A": {
        "category": "Opticien",
        "keywords": ["opticien", "magasin optique", "lunettes", "optique"],
    },

    # Hébergement / restauration
    "55.10Z": {
        "category": "Hôtel",
        "keywords": ["hôtel", "hotel", "hôtel restaurant", "boutique hôtel", "hébergement hôtelier"],
    },
    "55.20Z": {
        "category": "Gîte / hébergement touristique",
        "keywords": ["gîte", "chambre d'hôtes", "location vacances", "hébergement touristique", "résidence de tourisme"],
    },
    "55.30Z": {
        "category": "Camping",
        "keywords": ["camping", "camping caravaning", "camping-car", "parc résidentiel de loisirs"],
    },
    "56.10A": {
        "category": "Restaurant",
        "keywords": ["restaurant", "restauration traditionnelle", "brasserie", "bistrot", "restaurant français"],
    },
    "56.10B": {
        "category": "Cafétéria",
        "keywords": ["cafétéria", "self service", "restaurant self-service", "libre service restauration"],
    },
    "56.10C": {
        "category": "Restauration rapide",
        "keywords": ["fast food", "restauration rapide", "snack", "kebab", "burger", "pizzeria", "tacos", "sandwicherie"],
    },
    "56.21Z": {
        "category": "Traiteur",
        "keywords": ["traiteur", "service traiteur", "traiteur événementiel", "catering"],
    },
    "56.30Z": {
        "category": "Bar / café",
        "keywords": ["bar", "café", "pub", "bar à bière", "bar à vin", "brasserie", "débit de boissons"],
    },

    # Digital / médias
    "59.11B": {
        "category": "Production vidéo / publicité",
        "keywords": ["agence vidéo", "production vidéo", "film publicitaire", "vidéaste entreprise", "production audiovisuelle"],
    },
    "62.01Z": {
        "category": "Développement informatique",
        "keywords": ["développeur informatique", "agence web", "développement logiciel", "développement web", "éditeur logiciel", "ESN"],
    },
    "62.02A": {
        "category": "Conseil informatique",
        "keywords": ["consultant informatique", "conseil informatique", "intégrateur informatique", "ESN", "systèmes informatiques"],
    },

    # Finance / immobilier / professions intellectuelles
    "66.22Z": {
        "category": "Courtier en assurance",
        "keywords": ["courtier assurance", "courtier en assurances", "agence assurance", "agent assurance", "assureur"],
    },
    "68.31Z": {
        "category": "Agence immobilière",
        "keywords": ["agence immobilière", "agent immobilier", "immobilier", "transaction immobilière", "mandataire immobilier"],
    },
    "68.32A": {
        "category": "Syndic / gestion immobilière",
        "keywords": ["syndic", "syndic copropriété", "gestion immobilière", "administrateur de biens", "gestion locative"],
    },
    "69.10Z": {
        "category": "Juridique / avocat / notaire",
        "keywords": ["avocat", "cabinet avocat", "notaire", "étude notariale", "commissaire de justice", "cabinet juridique"],
    },
    "69.20Z": {
        "category": "Expert-comptable",
        "keywords": ["expert comptable", "cabinet comptable", "comptable", "cabinet expertise comptable", "audit comptable"],
    },
    "70.21Z": {
        "category": "Communication / relations publiques",
        "keywords": ["agence communication", "relations publiques", "agence RP", "communication entreprise", "attaché de presse"],
    },
    "70.22Z": {
        "category": "Conseil aux entreprises",
        "keywords": ["cabinet conseil", "consultant entreprise", "conseil en gestion", "conseil aux entreprises", "consulting"],
    },
    "71.11Z": {
        "category": "Architecture",
        "keywords": ["architecte", "cabinet architecture", "agence architecture", "architecte bâtiment"],
    },
    "71.12A": {
        "category": "Géomètre",
        "keywords": ["géomètre", "géomètre expert", "cabinet géomètre", "topographe"],
    },
    "71.12B": {
        "category": "Ingénierie / bureau d'études",
        "keywords": ["bureau études", "bureau d'études", "ingénierie", "ingénieur conseil", "études techniques"],
    },
    "71.20A": {
        "category": "Contrôle technique automobile",
        "keywords": ["contrôle technique", "centre contrôle technique", "contrôle technique automobile"],
    },
    "73.11Z": {
        "category": "Agence de publicité",
        "keywords": ["agence publicité", "agence marketing", "agence digitale", "publicité", "marketing digital", "agence créative"],
    },
    "74.10Z": {
        "category": "Design / graphisme",
        "keywords": ["designer", "graphiste", "agence design", "studio design", "studio graphique", "design produit"],
    },
    "74.20Z": {
        "category": "Photographie",
        "keywords": ["photographe", "studio photo", "photographe professionnel", "photographe entreprise", "photographe mariage"],
    },
    "75.00Z": {
        "category": "Vétérinaire",
        "keywords": ["vétérinaire", "clinique vétérinaire", "cabinet vétérinaire", "urgence vétérinaire"],
    },

    # RH / tourisme / sécurité / entretien
    "78.10Z": {
        "category": "Recrutement",
        "keywords": ["cabinet recrutement", "agence recrutement", "chasseur de tête", "recrutement"],
    },
    "78.20Z": {
        "category": "Intérim",
        "keywords": ["agence intérim", "intérim", "travail temporaire", "agence emploi"],
    },
    "79.11Z": {
        "category": "Agence de voyage",
        "keywords": ["agence de voyage", "voyages", "agence tourisme", "séjours"],
    },
    "80.10Z": {
        "category": "Sécurité privée",
        "keywords": ["sécurité privée", "société sécurité", "gardiennage", "agent sécurité", "entreprise surveillance"],
    },
    "80.20Z": {
        "category": "Alarme / systèmes de sécurité",
        "keywords": ["alarme", "installateur alarme", "vidéosurveillance", "caméra surveillance", "système sécurité", "télésurveillance"],
    },
    "81.21Z": {
        "category": "Nettoyage",
        "keywords": ["entreprise nettoyage", "société nettoyage", "nettoyage bureaux", "nettoyage professionnel", "ménage entreprise"],
    },
    "81.22Z": {
        "category": "Nettoyage industriel",
        "keywords": ["nettoyage industriel", "entreprise nettoyage industriel", "nettoyage bâtiment", "nettoyage chantier"],
    },
    "81.29A": {
        "category": "Désinsectisation / dératisation",
        "keywords": ["dératisation", "désinsectisation", "désinfection", "anti nuisibles", "exterminateur", "traitement nuisibles"],
    },
    "81.30Z": {
        "category": "Paysagiste",
        "keywords": ["paysagiste", "entreprise paysagiste", "entretien jardin", "aménagement paysager", "jardinier"],
    },

    # Formation / santé
    "85.51Z": {
        "category": "Sport / coaching",
        "keywords": ["coach sportif", "école de sport", "cours de sport", "club sport", "coaching sportif"],
    },
    "85.53Z": {
        "category": "Auto-école",
        "keywords": ["auto école", "auto-école", "école de conduite", "permis de conduire"],
    },
    "85.59A": {
        "category": "Formation professionnelle",
        "keywords": ["centre formation", "organisme formation", "formation professionnelle", "formation continue"],
    },
    "86.21Z": {
        "category": "Médecin généraliste",
        "keywords": ["médecin généraliste", "cabinet médical", "docteur", "médecin"],
    },
    "86.22C": {
        "category": "Médecin spécialiste",
        "keywords": ["médecin spécialiste", "cabinet spécialiste", "dermatologue", "cardiologue", "ophtalmologue", "gynécologue"],
    },
    "86.23Z": {
        "category": "Dentiste",
        "keywords": ["dentiste", "cabinet dentaire", "chirurgien dentiste", "centre dentaire"],
    },
    "86.90A": {
        "category": "Ambulance",
        "keywords": ["ambulance", "ambulancier", "transport sanitaire", "taxi ambulance"],
    },
    "86.90B": {
        "category": "Laboratoire d'analyses médicales",
        "keywords": ["laboratoire analyses", "laboratoire médical", "prise de sang", "biologie médicale"],
    },
    "86.90D": {
        "category": "Infirmier / sage-femme",
        "keywords": ["infirmier", "infirmière", "cabinet infirmier", "sage femme", "sage-femme"],
    },
    "86.90E": {
        "category": "Kinésithérapie / rééducation",
        "keywords": ["kinésithérapeute", "kiné", "cabinet kiné", "podologue", "pédicure podologue", "rééducation"],
    },
    "88.91A": {
        "category": "Crèche / petite enfance",
        "keywords": ["crèche", "micro crèche", "garderie", "petite enfance", "accueil enfants"],
    },

    # Loisirs / services personnels
    "93.11Z": {
        "category": "Installations sportives",
        "keywords": ["complexe sportif", "terrain sport", "centre sportif", "installation sportive"],
    },
    "93.13Z": {
        "category": "Salle de sport / fitness",
        "keywords": ["salle de sport", "fitness", "club fitness", "gym", "centre fitness", "musculation"],
    },
    "93.21Z": {
        "category": "Parc d'attractions",
        "keywords": ["parc attractions", "parc à thème", "parc de loisirs", "attractions"],
    },
    "95.11Z": {
        "category": "Réparation informatique",
        "keywords": ["réparation ordinateur", "dépannage informatique", "réparateur informatique", "maintenance informatique", "boutique informatique"],
    },
    "95.22Z": {
        "category": "Réparation électroménager",
        "keywords": ["réparation électroménager", "dépannage électroménager", "réparateur électroménager", "SAV électroménager"],
    },
    "96.01B": {
        "category": "Pressing / laverie",
        "keywords": ["pressing", "laverie", "laverie automatique", "blanchisserie", "teinturerie"],
    },
    "96.02A": {
        "category": "Coiffure",
        "keywords": ["coiffeur", "salon de coiffure", "coiffure", "barbier", "barbershop"],
    },
    "96.02B": {
        "category": "Institut de beauté",
        "keywords": ["institut beauté", "esthéticienne", "salon esthétique", "onglerie", "nail bar", "soins beauté"],
    },
    "96.03Z": {
        "category": "Pompes funèbres",
        "keywords": ["pompes funèbres", "funérarium", "services funéraires", "marbrerie funéraire"],
    },
    "96.04Z": {
        "category": "Bien-être / entretien corporel",
        "keywords": ["spa", "massage", "centre bien être", "institut massage", "entretien corporel"],
    },
}


PREFIX_PATTERNS = [
    r"^autres?\s+",
    r"^activités?\s+(?:des?|d['’])\s*",
    r"^services?\s+(?:des?|d['’])\s*",
    r"^travaux\s+(?:des?|d['’])\s*",
    r"^fabrication\s+(?:des?|d['’])\s*",
    r"^production\s+(?:des?|d['’])\s*",
    r"^construction\s+(?:des?|d['’])\s*",
    r"^commerce\s+de\s+détail\s+(?:des?|d['’])\s*",
    r"^commerce\s+de\s+gros\s+\(commerce interentreprises\)\s+(?:des?|d['’])\s*",
    r"^commerce\s+de\s+gros\s+(?:des?|d['’])\s*",
    r"^réparation\s+(?:des?|d['’])\s*",
    r"^entretien\s+et\s+réparation\s+(?:des?|d['’])\s*",
    r"^location\s+et\s+location-bail\s+(?:des?|d['’])\s*",
    r"^location\s+(?:des?|d['’])\s*",
    r"^gestion\s+(?:des?|d['’])\s*",
    r"^enseignement\s+(?:des?|d['’])\s*",
    r"^collecte\s+(?:des?|d['’])\s*",
    r"^transports?\s+(?:des?|d['’])\s*",
]


def clean_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" ,;.-")


def normalize_display(s: str) -> str:
    s = html.unescape(s)
    s = s.replace("’", "'")
    return clean_spaces(s)


def strip_nca(s: str) -> str:
    s = re.sub(r"\bn\.?\s*c\.?\s*a\.?\b", "", s, flags=re.I)
    return clean_spaces(s)


def simplify_label(label: str) -> str:
    s = strip_nca(label.lower())
    s = re.sub(r"\([^)]*\)", "", s)
    s = clean_spaces(s)
    for pat in PREFIX_PATTERNS:
        simplified = re.sub(pat, "", s, flags=re.I)
        if simplified != s and len(simplified) >= 3:
            s = simplified
            break
    return clean_spaces(s)


def add_keyword(target: list[str], value: str) -> None:
    value = normalize_display(value)
    if not value or len(value) < 3:
        return
    key = value.casefold()
    if key not in {x.casefold() for x in target}:
        target.append(value)


def generated_keywords(label: str) -> list[str]:
    """Crée des variantes prudentes à partir du libellé officiel."""
    label = normalize_display(label)
    keywords: list[str] = []

    # Libellé officiel nettoyé.
    add_keyword(keywords, strip_nca(label).lower())

    # Variante sans préfixe administratif.
    simple = simplify_label(label)
    add_keyword(keywords, simple)

    # Découpage des listes ; utile pour les libellés composés.
    no_parentheses = clean_spaces(re.sub(r"\([^)]*\)", "", strip_nca(label.lower())))
    for chunk in re.split(r"\s*;\s*|\s*,\s*", no_parentheses):
        chunk = clean_spaces(chunk)
        if 4 <= len(chunk) <= 80:
            add_keyword(keywords, chunk)

    # Quelques transformations génériques raisonnables.
    transforms = [
        ("activités photographiques", ["photographe", "photographie"]),
        ("activités vétérinaires", ["vétérinaire", "clinique vétérinaire"]),
        ("activités d'architecture", ["architecte", "cabinet d'architecture"]),
        ("activités comptables", ["comptable", "expert comptable"]),
        ("activités juridiques", ["cabinet juridique"]),
        ("restauration traditionnelle", ["restaurant"]),
        ("restauration de type rapide", ["restauration rapide", "fast food"]),
        ("débits de boissons", ["bar", "café"]),
        ("coiffure", ["coiffeur", "salon de coiffure"]),
        ("soins de beauté", ["institut de beauté", "esthéticienne"]),
        ("agences immobilières", ["agence immobilière", "agent immobilier"]),
        ("pratique dentaire", ["dentiste", "cabinet dentaire"]),
        ("services des traiteurs", ["traiteur"]),
        ("services d'aménagement paysager", ["paysagiste"]),
        ("travaux de plâtrerie", ["plaquiste", "plâtrier"]),
        ("travaux de charpente", ["charpentier"]),
        ("travaux de maçonnerie", ["maçon", "maçonnerie"]),
        ("travaux de couverture", ["couvreur", "toiture"]),
        ("travaux de peinture", ["peintre bâtiment"]),
        ("travaux d'installation électrique", ["électricien"]),
        ("travaux d'installation d'eau", ["plombier"]),
        ("entretien et réparation de véhicules automobiles", ["garage automobile", "garagiste"]),
        ("services de déménagement", ["déménageur", "déménagement"]),
        ("transports de voyageurs par taxis", ["taxi"]),
        ("activités des agences de voyage", ["agence de voyage"]),
        ("activités de sécurité privée", ["sécurité privée", "gardiennage"]),
        ("nettoyage courant des bâtiments", ["entreprise nettoyage"]),
        ("enseignement de la conduite", ["auto-école"]),
    ]
    low = label.casefold()
    for needle, values in transforms:
        if needle.casefold() in low:
            for value in values:
                add_keyword(keywords, value)

    return keywords[:12]


def fetch_source(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 NAFBusinessCategoriesBuilder/1.0",
            "Accept": "application/xml,text/xml,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def read_source(source: str | None) -> bytes:
    if source:
        path = Path(source)
        if path.exists():
            return path.read_bytes()
        if source.startswith(("http://", "https://")):
            return fetch_source(source)
        raise FileNotFoundError(f"Source introuvable : {source}")
    return fetch_source(SOURCE_URL)


def parse_naf_subclasses(xml_bytes: bytes) -> list[tuple[str, str]]:
    root = ET.fromstring(xml_bytes)
    title_attr = f"{{{DC_NS}}}title"

    target = None
    for simple_type in root.findall(f".//{{{XS_NS}}}simpleType"):
        if simple_type.attrib.get("name") == "SousClasseNAF2008Type":
            target = simple_type
            break

    if target is None:
        raise RuntimeError("Type SousClasseNAF2008Type introuvable dans le fichier INSEE.")

    rows: list[tuple[str, str]] = []
    seen: set[str] = set()

    for enum in target.findall(f".//{{{XS_NS}}}enumeration"):
        code = enum.attrib.get("value", "").strip().upper()
        title = enum.attrib.get(title_attr, "").strip()
        if not code or not title or code in seen:
            continue
        # Format attendu d'une sous-classe : 00.00X
        if not re.fullmatch(r"\d{2}\.\d{2}[A-Z]", code):
            continue
        seen.add(code)
        rows.append((code, normalize_display(title)))

    return rows


def build_dataset(rows: list[tuple[str, str]]) -> dict[str, dict[str, object]]:
    data: dict[str, dict[str, object]] = {}

    for code, official_label in rows:
        override = OVERRIDES.get(code)

        if override:
            category = str(override["category"])
            keywords = list(override["keywords"])

            # Garde aussi le libellé INSEE comme filet de sécurité.
            for kw in generated_keywords(official_label):
                add_keyword(keywords, kw)
        else:
            category = official_label
            keywords = generated_keywords(official_label)

        data[code] = {
            "category": category,
            "keywords": keywords,
        }

    return data


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Génère les 732 activités NAF rév.2 en JSON pour un scraper de prospects."
    )
    parser.add_argument(
        "-o",
        "--output",
        default="business_categories.json",
        help="Fichier JSON de sortie (défaut : business_categories.json)",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="URL ou fichier local naf-enum.xsd. Sans option : source officielle INSEE.",
    )
    parser.add_argument(
        "--save-source",
        default=None,
        help="Enregistre une copie locale du XSD téléchargé.",
    )
    args = parser.parse_args()

    xml_bytes = read_source(args.source)

    if args.save_source:
        Path(args.save_source).write_bytes(xml_bytes)

    rows = parse_naf_subclasses(xml_bytes)

    if len(rows) != EXPECTED_COUNT:
        raise RuntimeError(
            f"Nombre inattendu de sous-classes NAF : {len(rows)} au lieu de {EXPECTED_COUNT}. "
            "La source a peut-être changé."
        )

    data = build_dataset(rows)

    out = Path(args.output)
    out.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"OK : {len(data)} activités écrites dans {out}")
    print(f"Exemple 43.22B : {json.dumps(data['43.22B'], ensure_ascii=False, indent=2)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        raise
