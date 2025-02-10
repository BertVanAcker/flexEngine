#**************************************************************************
# * Copyright (C) 2025-present Bert Van Acker (B.MKR) <bva.bmkr@gmail.com>
# *
# * This file is part of the flexIA project.
# *
# * flexIA can not be copied and/or distributed without the express
# * permission of Bert Van Acker
# *************************************************************************
from setuptools import setup, find_packages

from flexEngine import (
    __author__,
    __description__,
    __email__,
    __license__,
    __title__,
    __version__,
    __url__
)

#--- insert device dependent setup ---
setup(
    name=__title__,
    version=__version__,
    description=__description__,
    long_description=open("README.md").read(),
    author=__author__,
    author_email=__email__,
    url=__url__,
    license=__license__,
    #install_requires=__install_requires__,
    python_requires=">=3.10",
    packages=find_packages(include=["flexEngine", "flexEngine.*"]),
    #package_data={'rpio': ['transformations/templates/*.template']},  # directory which contains the jinja templates
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