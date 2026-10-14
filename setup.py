from setuptools import setup, find_packages

setup(
    name='voxie-sync',
    version='1.0.0',
    license='AGPL-3.0-or-later',
    packages=find_packages(),
    # The setup page's files (libra_cli/setup_web/static).
    package_data={'libra_cli': ['setup_web/static/*', 'setup_web/static/fonts/*']},
    install_requires=[
        'click', # We use Click to make beautiful terminal prompts
    ],
    entry_points='''
        [console_scripts]
        voxie=libra_cli.main:cli
        libra-sync=libra_cli.main:cli
    ''',
)
