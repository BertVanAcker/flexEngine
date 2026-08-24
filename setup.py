#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
import os

from setuptools import setup, find_packages

#--- read package metadata without importing flexEngine (keeps this
#    working under isolated PEP 517 builds, where the source tree isn't
#    necessarily importable yet) ---
about = {}
with open(os.path.join(os.path.dirname(__file__), "flexEngine", "__init__.py")) as f:
    exec(f.read(), about)

#--- insert device dependent setup ---
setup(
    name=about["__title__"],
    version=about["__version__"],
    description=about["__description__"],
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author=about["__author__"],
    author_email=about["__email__"],
    url=about["__url__"],
    license=about["__license__"],
    #install_requires=__install_requires__,
    python_requires=">=3.10",
    packages=find_packages(include=["flexEngine", "flexEngine.*"]),
    package_data={"flexEngine": ["templates/*.tmpl"]},
    include_package_data=True,
    install_requires = [
        line.strip() for line in open('requirements.txt')],
    entry_points={
        "console_scripts": [
            "flexEngine = flexEngine.__main__:main",
        ]
    },
    classifiers=[
        "Intended Audience :: Developers",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
    ],
    keywords=[
        "Industrial automation",
        "Deployment",
        "Over-the-air updater",
    ],
)