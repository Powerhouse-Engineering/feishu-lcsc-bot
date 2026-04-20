from io import BytesIO

from flask import Flask, abort, request, send_file

from lcsc_step_downloader.core import get_lcsc_model


app = Flask(__name__)

@app.route("/get_model", methods=['GET'])
@app.route("/get_model/<lcsc_id>", methods=['GET'])
def get_model(lcsc_id=None):
    if not lcsc_id:
        lcsc_id = request.args["lcsc_id"]

    print(lcsc_id)
    name, data = get_lcsc_model(lcsc_id)
    if name and data:
        buffer = BytesIO()
        buffer.write(data)
        buffer.seek(0)
        return send_file(
            buffer,
            as_attachment=True,
            download_name=name,
            mimetype="application/step",
        )
    else:
        abort(404)

@app.route("/")
def index():
    return """<title>LCSC STEP downloader</title><h2>LCSC STEP file downloader</h2><form action="/get_model" method="GET">LCSC ID: <input type="text" name="lcsc_id"> <input type="submit" value="Download"></form>"""


if __name__ == "__main__":
    app.run()
